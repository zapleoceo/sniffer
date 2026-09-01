"""Осторожный догон истории групп в `raw_messages`.

Коллектор читает только последние сообщения после курсора. Сначала пачка
дедуплицируется в БД, затем в той же транзакции сдвигается курсор: падение между
этими шагами означало бы безвозвратно пропущенные объявления.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol

import structlog

from sniffer.domain.records import Chat, RawMessage
from sniffer.sources.telegram_discover_reference import MessageLike

log = structlog.get_logger(__name__)

HISTORY_CHATS_PER_TICK = 10
HISTORY_MESSAGES_PER_CHAT = 200


class HistoryReader(Protocol):
    async def history(
        self, entity: int | str, *, limit: int, min_id: int = 0, max_id: int = 0
    ) -> Sequence[MessageLike]: ...


class HistoryStore(Protocol):
    async def active_chats(self, *, limit: int) -> list[Chat]: ...

    async def store(self, chat: Chat, messages: Sequence[RawMessage], cursor: int) -> int: ...


Discover = Callable[[Sequence[MessageLike], str], Awaitable[int]]


@dataclass(slots=True)
class HistorySyncer:
    """Один проход по активным чатам: чтение, дедуп, курсор, перекрёстные ссылки."""

    reader: HistoryReader
    store: HistoryStore
    discover: Discover

    async def sync(self) -> int:
        inserted = 0
        for chat in await self.store.active_chats(limit=HISTORY_CHATS_PER_TICK):
            try:
                messages = await self.reader.history(
                    chat.username or chat.tg_id,
                    limit=HISTORY_MESSAGES_PER_CHAT,
                    min_id=chat.last_msg_id,
                )
                cursor = max((message.id for message in messages), default=chat.last_msg_id)
                inserted += await self.store.store(chat, to_raw(chat, messages), cursor)
                discovered = await self.discover(messages, chat.username or str(chat.tg_id))
                log.info(
                    "collector.history_synced",
                    chat=chat.tg_id,
                    read=len(messages),
                    inserted=inserted,
                    discovered=discovered,
                )
            except Exception as exc:
                # Один закрытый/удалённый чат не должен останавливать остальные.
                # Курсор при ошибке не сдвинут: на следующем проходе дочитаем.
                log.warning(
                    "collector.history_failed",
                    chat=chat.tg_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
        return inserted


def to_raw(chat: Chat, messages: Sequence[MessageLike]) -> list[RawMessage]:
    """Только пригодные к хранению посты; сервисные и пустые не засоряют сырьё."""
    raw: list[RawMessage] = []
    for message in messages:
        text = str(message.message or "").strip()
        posted_at = getattr(message, "date", None)
        if not text or not isinstance(posted_at, datetime):
            continue
        if posted_at.tzinfo is None:
            posted_at = posted_at.replace(tzinfo=UTC)
        raw.append(
            RawMessage(
                chat_tg_id=chat.tg_id,
                msg_id=message.id,
                text=text,
                text_hash=sha256(text.encode()).hexdigest(),
                posted_at=posted_at,
                # `raw_messages.seller_id` — внутренний PK таблицы sellers,
                # а Telegram sender_id — другой namespace. Связывать их
                # сможет извлекатель после нормализации продавца, не коллектор.
                seller_id=None,
                has_media=getattr(message, "media", None) is not None,
            )
        )
    return raw
