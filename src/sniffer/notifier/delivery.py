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

from sniffer.domain.records import OutboxMessage
from sniffer.notifier.digest import SEPARATOR, header, split
from sniffer.notifier.outcome import THREAD_GONE, Failure, classify
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

Sender = Callable[[int, str], Awaitable[None]]
# Отправка в тему личного чата: третьим аргументом `message_thread_id`.
ThreadSender = Callable[[int, str, int], Awaitable[None]]
# Приписка к сообщению, ушедшему в General вместо исчезнувшей темы: один раз, потому что
# связь после этого утрачена и следующие карточки темы уже не ищут.
LOST_NOTE = (
    "Вкладка этого поиска недоступна (её удалили?), поэтому пишу сюда. "
    "Открыть вкладку заново: /watch → поиск → «Вкладка»."
)
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
    thread_id: int | None = None
    note: str = ""


class Delivery:
    """Один проход очереди. Возврат — сколько сообщений ушло."""

    def __init__(
        self,
        send: Sender,
        *,
        send_in_thread: ThreadSender | None = None,
        pause_s: float = SEND_PAUSE_S,
        clock: Clock = _utcnow,
        scope: Scope = work_scope,
        policy: Policy | None = None,
    ) -> None:
        self._send = send
        self._send_in_thread = send_in_thread
        self._lost: set[int] = set()
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
        self._lost.clear()
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
            threads = await _threads(work, pending)
            await work.commit()
        sent, called = 0, False
        for unit in _units(pending, threads):
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
            # Тема, потерянная ранее в ЭТОМ проходе: очередь планировалась до отказа, и вторая
            # карточка поиска не должна ни стучаться в мёртвую тему, ни повторять приписку.
            thread = None if self._is_lost(held) else unit.thread_id
            failure = await self._attempt(Unit(held, unit.part, unit.parts, thread))
            if failure is not None and failure.reason == THREAD_GONE and thread:
                failure = await self._to_general(work, Unit(held, unit.part, unit.parts))
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
            await self._dispatch(unit, text)
        except BaseException as exc:
            return classify(exc)
        return None

    def _is_lost(self, held: list[OutboxMessage]) -> bool:
        subscriptions = _subscriptions(held)
        return bool(subscriptions) and subscriptions <= self._lost

    async def _dispatch(self, unit: Unit, text: str) -> None:
        recipient = unit.messages[0].recipient_id
        if unit.thread_id is not None and self._send_in_thread is not None:
            await self._send_in_thread(recipient, text, unit.thread_id)
        else:
            await self._send(recipient, text)

    async def _to_general(self, work: Work, unit: Unit) -> Failure | None:
        """Тема исчезла: связь утрачена, сообщение уходит без темы с приписью.

        Связь переводится в `lost` ДО повторной отправки: упади повтор, следующая попытка
        строки уже не станет искать исчезнувшую тему.
        """
        if work.tabs is not None:
            for sub in _subscriptions(unit.messages):
                await work.tabs.mark_lost(sub)
                self._lost.add(sub)
        return await self._attempt(Unit(unit.messages, unit.part, unit.parts, None, LOST_NOTE))

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
        body = render_digest(payloads, part=unit.part, parts=unit.parts)
    else:
        body = render(unit.messages[0].payload)
    return f"{escape(unit.note)}\n\n{body}" if unit.note else body


def render(payload: dict[str, Any]) -> str:
    """Карточка из данных очереди. Разметка собирается при отправке.

    Всё, что приехало из чужого чата, проходит через `escape`: текст
    объявления писал незнакомый человек, а Bot API принимает HTML.
    """
    if payload.get("kind") == "collection_result":
        return _collection_result(payload)
    title = escape(_clip(str(payload.get("title") or "без заголовка"), TITLE_LIMIT))
    url = escape(str(payload.get("url") or ""))
    price = _price(payload)
    lines = [f"<b>{title}</b>", price]
    summary = str(payload.get("summary") or "").strip()
    if summary:
        lines.append(escape(summary[:300]))
    if url:
        lines.append(f'<a href="{url}">открыть оригинал</a>')
    return "\n".join(line for line in lines if line)


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


def _subscriptions(messages: list[OutboxMessage]) -> set[int]:
    return {m.subscription_id for m in messages if m.subscription_id is not None}


async def _threads(work: Work, pending: list[OutboxMessage]) -> dict[int, int]:
    """Живые темы подписок прохода одним запросом; без `work.tabs` — тем нет."""
    ids = [m.subscription_id for m in pending if m.subscription_id is not None]
    return {} if work.tabs is None else await work.tabs.threads_for(ids)


def _units(messages: list[OutboxMessage], threads: dict[int, int] | None = None) -> list[Unit]:
    """Что уходит за проход и в каком порядке: одиночные карточки, затем подборки клиентов.

    Подборка клиента режется на сообщения по границе карточки (`digest.split`),
    поэтому текст карточек здесь собирается заранее — чтобы измерить. `render` не
    бросает на чужих данных (это проверяет тест), иначе одна плохая карточка роняла
    бы планирование всего прохода, а не только свою отправку под охраной.
    """
    units: list[Unit] = []
    # Подборка — на пару (клиент, тема): карточки двух поисков в разных темах не склеиваются.
    digests: dict[tuple[int, int | None], list[OutboxMessage]] = {}
    found = threads or {}
    for message in messages:
        thread = found.get(message.subscription_id) if message.subscription_id else None
        if message.payload.get("delivery_mode") == "digest":
            digests.setdefault((message.user_id, thread), []).append(message)
        else:
            units.append(Unit([message], thread_id=thread))
    for (_, thread), batch in digests.items():
        parts = split([render(message.payload) for message in batch])
        for number, group in enumerate(parts, start=1):
            units.append(Unit([batch[index] for index in group], number, len(parts), thread))
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
