"""Доставка из `outbox` в Telegram. Троттлинг и повтор без потери сообщения.

Почему отдельно от бота: доставка обязана переживать перезапуск диалогового
процесса, поэтому очередь лежит в таблице, а не в памяти. Почему отдельным
процессом: сорок сообщений подряд отключают бота в первые сутки, и темп
доставки нельзя ставить в зависимость от того, занят ли бот разговором.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

import structlog

from sniffer.db.engine import session_scope
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.domain.records import OutboxMessage

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


class Delivery:
    """Один проход очереди. Возврат — сколько сообщений ушло."""

    def __init__(self, send: Sender, *, pause_s: float = SEND_PAUSE_S) -> None:
        self._send = send
        self._pause_s = pause_s

    async def tick(self, *, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        sent = 0
        async with session_scope() as session:
            repo = DeliveryRepository(session)
            groups = _groups(await repo.take_pending(limit=BATCH, now=moment))
            for index, messages in enumerate(groups):
                if index:
                    await asyncio.sleep(self._pause_s)
                sent += await self._deliver_many(repo, messages, moment=moment)
            await session.commit()
        return sent

    async def _deliver(
        self, repo: DeliveryRepository, message: OutboxMessage, *, moment: datetime
    ) -> bool:
        return bool(await self._deliver_many(repo, [message], moment=moment))

    async def _deliver_many(
        self,
        repo: DeliveryRepository,
        messages: list[OutboxMessage],
        *,
        moment: datetime,
    ) -> int:
        if not messages:
            return 0
        text = render(messages[0].payload)
        if len(messages) > 1:
            text = render_digest([message.payload for message in messages])
        try:
            await self._send(messages[0].user_id, text)
        except Exception as exc:
            # Широкий except намеренно: причин не доставить сообщение столько
            # же, сколько состояний у чужого сервиса, и перечислять их значит
            # однажды уронить весь проход на неназванной. Решает не тип ошибки,
            # а счётчик попыток.
            for message in messages:
                if message.attempts + 1 >= MAX_ATTEMPTS:
                    await repo.give_up(message.id)
                    log.warning(
                        "notifier.gave_up",
                        message=message.id,
                        attempts=message.attempts + 1,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    continue
                await repo.mark_failed(message.id, retry_at=moment + RETRY_AFTER)
            log.info(
                "notifier.retry_later",
                messages=[message.id for message in messages],
                error=f"{type(exc).__name__}: {exc}",
            )
            return 0
        for message in messages:
            await repo.mark_sent(message.id, now=moment)
        return len(messages)


def render(payload: dict[str, Any]) -> str:
    """Карточка из данных очереди. Разметка собирается при отправке.

    Всё, что приехало из чужого чата, проходит через `escape`: текст
    объявления писал незнакомый человек, а Bot API принимает HTML.
    """
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
    amount = str(payload.get("price_amount") or "").strip()
    if not amount:
        return "цена не указана"
    currency = str(payload.get("price_currency") or "").strip()
    whole = amount.split(".")[0]
    pretty = f"{int(whole):,}".replace(",", " ") if whole.isdigit() else escape(whole)
    return escape(f"{pretty} {currency}".strip())
