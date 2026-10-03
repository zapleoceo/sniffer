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

Sender = Callable[[int, str], Awaitable[None]]
Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Step(NamedTuple):
    """Итог одной отправки: сколько сообщений ушло, звали ли Bot API, пора ли кончать проход."""

    sent: int
    called: bool
    halt: bool = False


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
            pending = await work.queue.take_pending(limit=BATCH, now=moment)
            await work.commit()
        sent, called = 0, False
        for messages in _groups(pending):
            if called:
                # Пауза нужна между обращениями к Bot API. Строка, которую успела
                # забрать другая копия, к Telegram не ходила, и ждать после неё незачем.
                await asyncio.sleep(self._pause_s)
            step = await self._deliver(messages, moment=moment)
            sent, called = sent + step.sent, step.called
            if step.halt:
                break
        return sent

    async def _deliver(self, messages: list[OutboxMessage], *, moment: datetime) -> Step:
        async with self._scope() as work:
            held = await work.queue.lock_pending([message.id for message in messages], now=moment)
            if not held:
                # Другая копия успела раньше или строку отложили: слать нечего.
                return Step(sent=0, called=False)
            failure = await self._attempt(held)
            if failure is not None:
                return await self._fail(work, held, failure)
            # Время берём ПОСЛЕ ответа Telegram: это момент отправки, а не начало
            # прохода, у которого за двадцать сообщений набегают десятки секунд.
            confirmed = self._clock()
            for message in held:
                await work.queue.mark_sent(message.id, now=confirmed)
            await work.commit()
        return Step(sent=len(held), called=True)

    async def _attempt(self, messages: list[OutboxMessage]) -> Failure | None:
        """Шаги отправки под одной охраной, последний `except` — корень иерархии.

        Чужой код здесь два: сборка текста из данных очереди и сам Bot API. Что бы
        из них ни вылетело, итог — документированный исход, а не трейсбек. Что из
        этого просьба остановиться, решает `classify`: шаг не выбирает сам.
        """
        try:
            text = _text(messages)
            await self._send(messages[0].recipient_id, text)
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


def _text(messages: list[OutboxMessage]) -> str:
    """Текст одной отправки: подборка для нескольких карточек, иначе одна карточка."""
    if len(messages) > 1:
        return render_digest([message.payload for message in messages])
    return render(messages[0].payload)


def render(payload: dict[str, Any]) -> str:
    """Карточка из данных очереди. Разметка собирается при отправке.

    Всё, что приехало из чужого чата, проходит через `escape`: текст
    объявления писал незнакомый человек, а Bot API принимает HTML.
    """
    if payload.get("kind") == "collection_result":
        return _collection_result(payload)
    title = escape(str(payload.get("title") or "без заголовка"))
    url = escape(str(payload.get("url") or ""))
    price = _price(payload)
    lines = [f"<b>{title}</b>", price]
    summary = str(payload.get("summary") or "").strip()
    if summary:
        lines.append(escape(summary[:300]))
    if url:
        lines.append(f'<a href="{url}">открыть оригинал</a>')
    return "\n".join(line for line in lines if line)


def _collection_result(payload: dict[str, Any]) -> str:
    """Render a deferred answer from data, never from stored HTML."""
    intro = escape(str(payload.get("intro") or "Обновление каталога завершено."))
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raw_items = []
    cards = [render(item) for item in raw_items if isinstance(item, dict)]
    return "\n\n".join([intro, *cards])


def render_digest(payloads: list[dict[str, Any]]) -> str:
    """Одна подборка вместо серии сообщений в одну секунду."""
    cards = [render(payload) for payload in payloads]
    return "<b>Новые находки по вашему запросу</b>\n\n" + "\n\n".join(cards)


def _groups(messages: list[OutboxMessage]) -> list[list[OutboxMessage]]:
    grouped: list[list[OutboxMessage]] = []
    digest_by_user: dict[int, list[OutboxMessage]] = {}
    for message in messages:
        if message.payload.get("delivery_mode") == "digest":
            digest_by_user.setdefault(message.user_id, []).append(message)
        else:
            grouped.append([message])
    grouped.extend(digest_by_user.values())
    return grouped


def _price(payload: dict[str, Any]) -> str:
    display = str(payload.get("price_display") or "").strip()
    if display:
        return escape(display)
    amount = str(payload.get("price_amount") or "").strip()
    if not amount:
        return "цена не указана"
    currency = str(payload.get("price_currency") or "").strip()
    whole = amount.split(".")[0]
    pretty = f"{int(whole):,}".replace(",", " ") if whole.isdigit() else escape(whole)
    return escape(f"{pretty} {currency}".strip())
