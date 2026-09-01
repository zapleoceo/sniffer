"""Справочник отслеживаемых сообществ."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, or_, select, update

from sniffer.db import models
from sniffer.db.mappers import to_chat
from sniffer.db.repositories.base import Repository
from sniffer.domain.records import Chat


class ChatRepository(Repository):
    async def has_identity(self, *, tg_id: int | None = None, username: str = "") -> bool:
        """Есть ли уже чат с этим Telegram id или публичным именем."""
        predicates = []
        if tg_id is not None:
            predicates.append(models.Chat.tg_id == tg_id)
        if username:
            predicates.append(func.lower(models.Chat.username) == username.lstrip("@").lower())
        if not predicates:
            return False
        statement = select(models.Chat.id).where(or_(*predicates)).limit(1)
        return bool(await self._session.scalar(statement))

    async def count(self) -> int:
        return int(await self._session.scalar(select(func.count(models.Chat.id))) or 0)

    async def get_by_tg_id(self, tg_id: int) -> Chat | None:
        row = await self._session.scalar(select(models.Chat).where(models.Chat.tg_id == tg_id))
        return to_chat(row) if row is not None else None

    async def list_active(self, *, limit: int = 10) -> list[Chat]:
        """Самые плотные по объявлениям чаты первыми.

        За один живой поиск обходим не больше десяти чатов (architecture.md,
        раздел 10), поэтому порядок здесь решает, что клиент вообще увидит.
        """
        rows = await self._session.scalars(
            select(models.Chat)
            .where(models.Chat.is_active.is_(True))
            .order_by(models.Chat.search_rank, models.Chat.id)
            .limit(limit)
        )
        return [to_chat(row) for row in rows]

    async def list_all(self, *, limit: int = 200) -> list[Chat]:
        """Весь реестр для показа владельцу, свежие записи первыми.

        Отдельно от `list_active`: та отвечает на вопрос «где искать сейчас» и
        режется десяткой по бюджету плана. Здесь вопрос другой — «что вообще
        накопилось», и выключенный чат в ответе обязан быть виден.
        """
        rows = await self._session.scalars(
            select(models.Chat).order_by(models.Chat.id.desc()).limit(limit)
        )
        return [to_chat(row) for row in rows]

    async def add(self, chat: Chat) -> Chat:
        row = models.Chat(
            tg_id=chat.tg_id,
            username=chat.username,
            title=chat.title,
            city=chat.city,
            categories=list(chat.categories),
            is_active=chat.is_active,
            search_rank=chat.search_rank,
            msg_count_24h=chat.msg_count_24h,
            last_msg_id=chat.last_msg_id,
            last_synced_at=chat.last_synced_at,
        )
        self._session.add(row)
        await self._session.flush()
        return to_chat(row)

    async def mark_synced(self, tg_id: int, last_msg_id: int) -> None:
        """До какого сообщения дочитали.

        Курсор двигается только вперёд (`greatest`): догон истории после
        простоя идёт пачками и вразнобой, а откат назад заставил бы
        перечитывать уже обработанное (architecture.md, раздел 10). Отметка
        времени обновляется всегда — «сходил и не нашёл нового» тоже сходил.
        """
        await self._session.execute(
            update(models.Chat)
            .where(models.Chat.tg_id == tg_id)
            .values(
                last_msg_id=func.greatest(models.Chat.last_msg_id, last_msg_id),
                last_synced_at=datetime.now(UTC),
            )
        )
