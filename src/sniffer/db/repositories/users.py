"""Клиенты бота."""

from __future__ import annotations

from typing import cast

from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sniffer.db import models
from sniffer.db.mappers import to_user
from sniffer.db.repositories.base import Repository
from sniffer.domain.records import User

# Дашборд — страница для одного человека, но список клиентов растёт. Потолок
# нужен, чтобы через год страница не начала тянуть всю таблицу.
PAGE_LIMIT = 200


class UserRepository(Repository):
    async def get_by_tg_id(self, tg_user_id: int) -> User | None:
        row = await self._session.scalar(
            select(models.User).where(models.User.tg_user_id == tg_user_id)
        )
        return to_user(row) if row is not None else None

    async def get(self, user_id: int) -> User | None:
        row = await self._session.get(models.User, user_id)
        return to_user(row) if row is not None else None

    async def recent(self, *, limit: int = PAGE_LIMIT) -> list[User]:
        """Клиенты, свежие сверху. Для таблицы пользователей в дашборде."""
        rows = await self._session.scalars(
            select(models.User)
            .order_by(models.User.created_at.desc(), models.User.id.desc())
            .limit(min(limit, PAGE_LIMIT))
        )
        return [to_user(row) for row in rows]

    async def get_or_create(
        self, tg_user_id: int, *, username: str | None = None, lang: str = "ru"
    ) -> User:
        """Первое сообщение от клиента заводит его запись.

        Через `ON CONFLICT DO NOTHING`, а не «проверил — вставил»: две команды
        подряд от одного человека обрабатываются разными апдейтами, и проверка
        существования проигрывает гонку уникальному индексу.
        """
        table = cast(Table, models.User.__table__)
        inserted = await self._session.scalar(
            pg_insert(table)
            .values(tg_user_id=tg_user_id, username=username, lang=lang)
            .on_conflict_do_nothing(index_elements=["tg_user_id"])
            .returning(table.c.id)
        )
        if inserted is None:
            existing = await self.get_by_tg_id(tg_user_id)
            if existing is None:  # pragma: no cover — конфликт был, строки нет
                raise LookupError(f"пользователь {tg_user_id} не найден после конфликта вставки")
            return existing

        row = await self._session.get(models.User, inserted)
        if row is None:  # pragma: no cover — строка вставлена в этой же транзакции
            raise LookupError(f"вставленный пользователь {tg_user_id} не читается")
        return to_user(row)
