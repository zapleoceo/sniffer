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
            for index, message in enumerate(await repo.take_pending(limit=BATCH, now=moment)):
                if index:
                    await asyncio.sleep(self._pause_s)
                if await self._deliver(repo, message, moment=moment):
                    sent += 1
            await session.commit()
        return sent

    async def _deliver(
        self, repo: DeliveryRepository, message: OutboxMessage, *, moment: datetime
    ) -> bool:
        try:
            await self._send(message.user_id, render(message.payload))
        except Exception as exc:
            # Широкий except намеренно: причин не доставить сообщение столько
            # же, сколько состояний у чужого сервиса, и перечислять их значит
            # однажды уронить весь проход на неназванной. Решает не тип ошибки,
            # а счётчик попыток.
            if message.attempts + 1 >= MAX_ATTEMPTS:
                await repo.give_up(message.id)
                log.warning(
                    "notifier.gave_up",
                    message=message.id,
                    attempts=message.attempts + 1,
                    error=f"{type(exc).__name__}: {exc}",
                )
                return False
            await repo.mark_failed(message.id, retry_at=moment + RETRY_AFTER)
            log.info(
                "notifier.retry_later",
                message=message.id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return False
        await repo.mark_sent(message.id, now=moment)
        return True


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


def _price(payload: dict[str, Any]) -> str:
    amount = str(payload.get("price_amount") or "").strip()
    if not amount:
        return "цена не указана"
    currency = str(payload.get("price_currency") or "").strip()
    whole = amount.split(".")[0]
    pretty = f"{int(whole):,}".replace(",", " ") if whole.isdigit() else escape(whole)
    return escape(f"{pretty} {currency}".strip())
