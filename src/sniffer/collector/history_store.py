"""Транзакционная запись истории Telegram в сырьё воронки."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db.engine import session_scope
from sniffer.db.repositories.chats import ChatRepository
from sniffer.db.repositories.listings import ListingRepository
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
            # Пустой проход тоже отмечается: «сходили и не нашли нового» — это
            # время, которое видит /database. Курсор при этом не отъезжает:
            # `mark_synced` берёт `greatest`. До исключения сюда не доходим.
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


class DatabaseLivenessStore:
    """Каталог для проверки живости: чьи карточки перечитать и какие погасить."""

    async def next_chat(self) -> Chat | None:
        async with _session() as session:
            return await ChatRepository(session).next_for_liveness()

    async def mark_checked(self, chat: Chat) -> None:
        async with _session() as session:
            await ChatRepository(session).mark_liveness_checked(chat.tg_id)

    async def live_refs(
        self, chat: Chat, *, since: datetime, after_id: int, limit: int
    ) -> list[tuple[int, int]]:
        async with _session() as session:
            return await ListingRepository(session).live_archive_refs(
                chat.tg_id, since=since, limit=limit, after_id=after_id
            )

    async def cursor(self, chat: Chat) -> int:
        async with _session() as session:
            return await ChatRepository(session).liveness_cursor(chat.tg_id)

    async def save_cursor(self, chat: Chat, listing_id: int) -> None:
        async with _session() as session:
            await ChatRepository(session).set_liveness_cursor(chat.tg_id, listing_id)

    async def retire(self, listing_ids: list[int], *, reason: str) -> int:
        async with _session() as session:
            return await ListingRepository(session).deactivate_many(listing_ids, reason=reason)
