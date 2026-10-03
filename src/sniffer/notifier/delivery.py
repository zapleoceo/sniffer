"""Доставка из `outbox` в Telegram. Троттлинг и повтор без потери сообщения.

Почему отдельно от бота: доставка обязана переживать перезапуск диалогового
процесса, поэтому очередь лежит в таблице, а не в памяти. Почему отдельным
процессом: сорок сообщений подряд отключают бота в первые сутки, и темп
доставки нельзя ставить в зависимость от того, занят ли бот разговором.

Транзакция — на ОДНО сообщение (на одну подборку, если это дайджест). Строки
запираются перед отправкой и помечаются отправленными сразу после ответа
Telegram: убитый посреди прохода процесс теряет не пачку, а не больше одного
сообщения, а `sent_at` — момент подтверждения Telegram, а не начало прохода.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any, NamedTuple

import structlog

from sniffer.domain.records import OutboxMessage
from sniffer.notifier.ports import Scope, Work, work_scope

log = structlog.get_logger(__name__)

# Пауза между сообщениями одного прохода. Telegram разрешает ~30 сообщений в
# секунду на бота, но клиенту важнее не получить очередь из пяти карточек
# подряд: секунда между ними читается как работа, а не как рассылка.
SEND_PAUSE_S = 1.0
BATCH = 20
# Сколько раз пробуем, прежде чем признать сообщение недоставляемым. Три —
# потому что первые две причины обычно временные (сеть, 429), а третья уже
# означает, что клиент заблокировал бота.
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=15)

Sender = Callable[[int, str], Awaitable[None]]
Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Step(NamedTuple):
    """Итог одной отправки: сколько сообщений ушло и звали ли мы Bot API."""

    sent: int
    called: bool


class Delivery:
    """Один проход очереди. Возврат — сколько сообщений ушло."""

    def __init__(
        self,
        send: Sender,
        *,
        pause_s: float = SEND_PAUSE_S,
        clock: Clock = _utcnow,
        scope: Scope = work_scope,
    ) -> None:
        self._send = send
        self._pause_s = pause_s
        self._clock = clock
        self._scope = scope

    async def tick(self, *, now: datetime | None = None) -> int:
        moment = now or self._clock()
        async with self._scope() as work:
            # Только чтение: коммита нет, и по коду видно, что проход ничего не менял.
            pending = await work.queue.take_pending(limit=BATCH, now=moment)
        sent, called = 0, False
        for messages in _groups(pending):
            if called:
                # Пауза нужна между обращениями к Bot API. Строка, которую успела
                # забрать другая копия, к Telegram не ходила, и ждать после неё незачем.
                await asyncio.sleep(self._pause_s)
            step = await self._deliver(messages, moment=moment)
            sent, called = sent + step.sent, step.called
        return sent

    async def _deliver(self, messages: list[OutboxMessage], *, moment: datetime) -> Step:
        async with self._scope() as work:
            held = await work.queue.lock_pending([message.id for message in messages], now=moment)
            if not held:
                # Другая копия успела раньше или строку отложили: слать нечего.
                return Step(sent=0, called=False)
            try:
                await self._send(held[0].recipient_id, _text(held))
            except Exception as exc:
                # Широкий except намеренно: причин не доставить сообщение столько
                # же, сколько состояний у чужого сервиса, и перечислять их значит
                # однажды уронить весь проход на неназванной. Решает не тип ошибки,
                # а счётчик попыток.
                await self._postpone(work, held, exc, moment=moment)
                return Step(sent=0, called=True)
            # Время берём ПОСЛЕ ответа Telegram: это момент отправки, а не начало
            # прохода, у которого за двадцать сообщений набегают десятки секунд.
            confirmed = self._clock()
            for message in held:
                await work.queue.mark_sent(message.id, now=confirmed)
            await work.commit()
        return Step(sent=len(held), called=True)

    async def _postpone(
        self, work: Work, messages: list[OutboxMessage], exc: Exception, *, moment: datetime
    ) -> None:
        for message in messages:
            if message.attempts + 1 >= MAX_ATTEMPTS:
                await work.queue.give_up(message.id)
                log.warning(
                    "notifier.gave_up",
                    message=message.id,
                    attempts=message.attempts + 1,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            await work.queue.mark_failed(message.id, retry_at=moment + RETRY_AFTER)
        await work.commit()
        log.info(
            "notifier.retry_later",
            messages=[message.id for message in messages],
            error=f"{type(exc).__name__}: {exc}",
        )


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
