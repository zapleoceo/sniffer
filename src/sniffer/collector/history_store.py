"""Транзакционная запись истории Telegram в сырьё воронки."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db.engine import session_scope
from sniffer.db.repositories.chats import ChatRepository
from sniffer.db.repositories.raw_messages import RawMessageRepository
from sniffer.domain.records import Chat, RawMessage


@asynccontextmanager
async def _session() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


class DatabaseHistoryStore:
    """Курсор и сырьё меняются одной транзакцией, иначе сообщения теряются."""

    async def active_chats(self, *, limit: int) -> list[Chat]:
        async with _session() as session:
            return await ChatRepository(session).list_active(limit=limit)

    async def store(self, chat: Chat, messages: Sequence[RawMessage], cursor: int) -> int:
        async with _session() as session:
            inserted = await RawMessageRepository(session).add_many(messages)
            if cursor > chat.last_msg_id:
                await ChatRepository(session).mark_synced(chat.tg_id, cursor)
            return len(inserted)
