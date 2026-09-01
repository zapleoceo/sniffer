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

    async def next_backfill(self) -> Chat | None:
        async with _session() as session:
            return await ChatRepository(session).next_backfill()

    async def store_archive(
        self, chat: Chat, messages: list[RawMessage], *, oldest_msg_id: int, done: bool
    ) -> int:
        """Страница архива и её курсор — одной транзакцией, как и у догона.

        Порядок тот же и по той же причине: сдвинуть курсор отдельно от вставки
        значит при падении между шагами потерять страницу навсегда — вниз к ней
        уже никто не вернётся.
        """
        async with _session() as session:
            inserted = await RawMessageRepository(session).add_many(messages)
            await ChatRepository(session).mark_backfilled(
                chat.tg_id, oldest_msg_id=oldest_msg_id, done=done
            )
            return len(inserted)
