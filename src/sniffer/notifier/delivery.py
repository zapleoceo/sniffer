"""Доставка из `outbox` в Telegram. Троттлинг и повтор без потери сообщения.

Почему отдельно от бота: доставка обязана переживать перезапуск диалогового
процесса, поэтому очередь лежит в таблице, а не в памяти. Почему отдельным
процессом: сорок сообщений подряд отключают бота в первые сутки, и темп
доставки нельзя ставить в зависимость от того, занят ли бот разговором.

Транзакция — на ОДНО сообщение (на одну подборку, если это дайджест). Строки
запираются перед отправкой и помечаются отправленными сразу после ответа
Telegram: убитый посреди прохода процесс теряет не пачку, а не больше одного
сообщения, а `sent_at` — момент подтверждения Telegram, а не начало прохода.

Исход отправки решают `outcome.classify` и `policy.decide`, а не этот файл: 403
(клиент заблокировал бота) отменяет его очередь, 429 останавливает нотифаер на
столько, сколько просит Telegram, 400 не повторяется, остальное ждёт с
нарастающей паузой и конечным числом попыток.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from html import escape
from typing import Any, NamedTuple

import structlog

from sniffer.domain.monitoring import OVERFLOW_KIND
from sniffer.domain.records import OutboxMessage
from sniffer.notifier.digest import SEPARATOR, header, split
from sniffer.notifier.outcome import Failure, classify
from sniffer.notifier.policy import MAX_ATTEMPTS, Action, Policy, Verdict, decide
from sniffer.notifier.ports import Scope, Work, work_scope

log = structlog.get_logger(__name__)

__all__ = ["BATCH", "MAX_ATTEMPTS", "Delivery", "Sender", "render", "render_digest"]

# Пауза между сообщениями одного прохода. Telegram разрешает ~30 сообщений в
# секунду на бота, но клиенту важнее не получить очередь из пяти карточек
# подряд: секунда между ними читается как работа, а не как рассылка.
SEND_PAUSE_S = 1.0
BATCH = 20
BLOCKED_NOTE = "bot_blocked"
# Заголовок писал незнакомый человек и длиной он не ограничен: без потолка одна
# карточка с заголовком на тысячи знаков занимала бы целое сообщение.
TITLE_LIMIT = 200
FACTS_LIMIT = 120

Sender = Callable[[int, str], Awaitable[None]]
Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Step(NamedTuple):
    """Итог одной отправки: сколько сообщений ушло, звали ли Bot API, пора ли кончать проход."""

    sent: int
    called: bool
    halt: bool = False


class Unit(NamedTuple):
    """Что уходит одним сообщением Telegram: строки очереди и место в подборке."""

    messages: list[OutboxMessage]
    part: int = 1
    parts: int = 1


class Delivery:
    """Один проход очереди. Возврат — сколько сообщений ушло."""

    def __init__(
        self,
        send: Sender,
        *,
        pause_s: float = SEND_PAUSE_S,
        clock: Clock = _utcnow,
        scope: Scope = work_scope,
        policy: Policy | None = None,
    ) -> None:
        self._send = send
        self._pause_s = pause_s
        self._clock = clock
        self._scope = scope
        self._policy = policy or Policy()
        # Пока не наступило, нотифаер не обращается к Bot API вовсе: 429 просит
        # подождать ВСЕХ, а не одного клиента. В памяти, а не в базе: рестарт
        # сотрёт паузу, и первый же запрос получит новый 429 с новым сроком.
        self._paused_until: datetime | None = None

    async def tick(self, *, now: datetime | None = None) -> int:
        if self._paused_until is not None and self._clock() < self._paused_until:
            return 0
        moment = now or self._clock()
        async with self._scope() as work:
            # Очередь тех, кто заблокировал бота, отменяется до выборки: её
            # наполняют и те, кто о блокировке не знает (матчер, сборщик ответов).
            await work.queue.cancel_for_blocked_users(reason=BLOCKED_NOTE)
            # Просроченное не доставляется вчерашним: после простоя нотифаера
            # «мгновенные» двухдневной давности — шум, а не новости.
            expired = await work.queue.cancel_expired(now=moment, ttl=self._policy.ttl)
            if expired:
                log.info("notifier.expired", cancelled=expired)
            pending = await work.queue.take_pending(limit=BATCH, now=moment)
            await work.commit()
        sent, called = 0, False
        for unit in _units(pending):
            if called:
                # Пауза нужна между обращениями к Bot API. Строка, которую успела
                # забрать другая копия, к Telegram не ходила, и ждать после неё незачем.
                await asyncio.sleep(self._pause_s)
            step = await self._deliver(unit, moment=moment)
            sent, called = sent + step.sent, step.called
            if step.halt:
                break
        return sent

    async def _deliver(self, unit: Unit, *, moment: datetime) -> Step:
        async with self._scope() as work:
            ids = [message.id for message in unit.messages]
            held = await work.queue.lock_pending(ids, now=moment)
            if not held:
                # Другая копия успела раньше или строку отложили: слать нечего.
                return Step(sent=0, called=False)
            failure = await self._attempt(Unit(held, unit.part, unit.parts))
            if failure is not None:
                return await self._fail(work, held, failure)
            # Время берём ПОСЛЕ ответа Telegram: это момент отправки, а не начало
            # прохода, у которого за двадцать сообщений набегают десятки секунд.
            confirmed = self._clock()
            for message in held:
                await work.queue.mark_sent(message.id, now=confirmed)
            await work.commit()
        return Step(sent=len(held), called=True)

    async def _attempt(self, unit: Unit) -> Failure | None:
        """Шаги отправки под одной охраной, последний `except` — корень иерархии.

        Чужой код здесь два: сборка текста из данных очереди и сам Bot API. Что бы
        из них ни вылетело, итог — документированный исход, а не трейсбек. Что из
        этого просьба остановиться, решает `classify`: шаг не выбирает сам.
        """
        try:
            text = _text(unit)
            await self._send(unit.messages[0].recipient_id, text)
        except BaseException as exc:
            return classify(exc)
        return None

    async def _fail(self, work: Work, held: list[OutboxMessage], failure: Failure) -> Step:
        now = self._clock()
        verdicts = [
            decide(failure, attempts=message.attempts, now=now, policy=self._policy)
            for message in held
        ]
        lead = verdicts[0]
        if lead.action is Action.PAUSE:
            # Сообщение не виновато: Telegram просит подождать или сломан токен.
            # Строка остаётся в очереди нетронутой, попытка ей не засчитывается.
            self._paused_until = lead.until
            log.warning("notifier.paused", until=lead.until, reason=lead.note)
            return Step(sent=0, called=True, halt=True)
        if lead.action is Action.BLOCK:
            await work.users.set_bot_blocked(held[0].recipient_id, blocked=True, at=now)
            cancelled = await work.queue.cancel_pending_of(held[0].user_id, reason=lead.note)
            log.info("notifier.recipient_blocked", user=held[0].user_id, cancelled=cancelled)
        else:
            for message, verdict in zip(held, verdicts, strict=True):
                await self._record(work, message, verdict)
        await work.commit()
        return Step(sent=0, called=True)

    async def _record(self, work: Work, message: OutboxMessage, verdict: Verdict) -> None:
        attempts = message.attempts + 1
        if verdict.action is Action.RETRY:
            assert verdict.until is not None
            await work.queue.mark_failed(message.id, retry_at=verdict.until, error=verdict.note)
            log.info("notifier.retry_later", message=message.id, attempts=attempts)
            return
        await work.queue.give_up(message.id, error=verdict.note)
        log.warning("notifier.gave_up", message=message.id, attempts=attempts, error=verdict.note)


def _text(unit: Unit) -> str:
    """Текст одной отправки: подборка, если карточек несколько или это часть подборки."""
    if len(unit.messages) > 1 or unit.parts > 1:
        payloads = [message.payload for message in unit.messages]
        return render_digest(payloads, part=unit.part, parts=unit.parts)
    return render(unit.messages[0].payload)


def render(payload: dict[str, Any]) -> str:
    """Карточка из данных очереди. Разметка собирается при отправке.

    Всё, что приехало из чужого чата, проходит через `escape`: текст
    объявления писал незнакомый человек, а Bot API принимает HTML.
    """
    if payload.get("kind") == "collection_result":
        return _collection_result(payload)
    if payload.get("kind") == OVERFLOW_KIND:
        return _overflow(payload)
    title = escape(_clip(str(payload.get("title") or "без заголовка"), TITLE_LIMIT))
    url = escape(str(payload.get("url") or ""))
    price = _price(payload)
    lines = [f"<b>{title}</b>", price]
    # Факты (марка, объём, год, пробег) собраны при постановке в очередь той же функцией, что и
    # карточка в чате (`domain.card_facts`): три поверхности не должны пересказывать их по-своему.
    facts = str(payload.get("facts") or "").strip()
    if facts:
        lines.append(escape(_clip(facts, FACTS_LIMIT)))
    summary = str(payload.get("summary") or "").strip()
    if summary:
        lines.append(escape(summary[:300]))
    if url:
        lines.append(f'<a href="{url}">открыть оригинал</a>')
    return "\n".join(line for line in lines if line)


def _overflow(payload: dict[str, Any]) -> str:
    """Сводка слота одной строкой: сколько подошло сверх суточного потолка.

    Числа приходят из очереди и не доверяются: нечисло превращается в ноль, а не в трейсбек
    на рендере (`render` не вправе бросать на чужих данных).
    """
    count = _whole(payload.get("count"))
    cap = _whole(payload.get("cap"))
    return (
        f"Сегодня подошло ещё {count} сверх {cap} в сутки. "
        "Сузьте запрос (цена, район, модель), чтобы не пропускать лучшее."
    )


def _whole(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _clip(text: str, limit: int) -> str:
    """Обрезка ДО экранирования: после неё `&amp;` пополам не режется."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _collection_result(payload: dict[str, Any]) -> str:
    """Render a deferred answer from data, never from stored HTML."""
    intro = escape(str(payload.get("intro") or "Обновление каталога завершено."))
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raw_items = []
    cards = [render(item) for item in raw_items if isinstance(item, dict)]
    return "\n\n".join([intro, *cards])


def render_digest(payloads: list[dict[str, Any]], *, part: int = 1, parts: int = 1) -> str:
    """Одна подборка вместо серии сообщений в одну секунду."""
    cards = [render(payload) for payload in payloads]
    return header(part, parts) + SEPARATOR + SEPARATOR.join(cards)


def _units(messages: list[OutboxMessage]) -> list[Unit]:
    """Что уходит за проход и в каком порядке: одиночные карточки, затем подборки клиентов.

    Подборка клиента режется на сообщения по границе карточки (`digest.split`),
    поэтому текст карточек здесь собирается заранее — чтобы измерить. `render` не
    бросает на чужих данных (это проверяет тест), иначе одна плохая карточка роняла
    бы планирование всего прохода, а не только свою отправку под охраной.
    """
    units: list[Unit] = []
    digests: dict[int, list[OutboxMessage]] = {}
    for message in messages:
        if message.payload.get("delivery_mode") == "digest":
            digests.setdefault(message.user_id, []).append(message)
        else:
            units.append(Unit([message]))
    for batch in digests.values():
        parts = split([render(message.payload) for message in batch])
        for number, group in enumerate(parts, start=1):
            units.append(Unit([batch[index] for index in group], number, len(parts)))
    return units


def _price(payload: dict[str, Any]) -> str:
    display = str(payload.get("price_display") or "").strip()
    if display:
        return escape(display)
    amount = str(payload.get("price_amount") or "").strip()
    if not amount:
        return "цена не указана"
    currency = str(payload.get("price_currency") or "").strip()
    whole = amount.split(".")[0]
    digits = whole.isascii() and whole.isdigit()  # у «²» isdigit() истинно, а int() падает
    pretty = f"{int(whole):,}".replace(",", " ") if digits else escape(whole)
    return escape(f"{pretty} {currency}".strip())
