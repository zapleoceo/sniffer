"""Клиенты бота."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import Table, func, or_, select, update
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

    async def set_bot_blocked(self, tg_user_id: int, *, blocked: bool, at: datetime) -> int | None:
        """Запомнить, что писать клиенту нельзя, или снять метку. Возврат — `users.id`.

        `None` — менять нечего: такого клиента у нас нет (человек нажал «Старт» и
        заблокировал бота, не написав ни слова — помнить о нём нечем и незачем) или
        снимать нечего.

        Блокировка повторяется (403 на каждую попытку, потом ещё апдейт
        `my_chat_member`), и вторая отметка не должна сдвигать момент первой:
        `coalesce` оставляет самую раннюю. Снятие безусловно по смыслу — клиент
        написал боту или разблокировал его, и бот ему доступен, — но пишет в строку
        только когда метка стоит: так его можно звать на КАЖДОЕ сообщение клиента,
        не плодя версий строки в таблице, которую бот читает на каждом сообщении.
        """
        statement = update(models.User).where(models.User.tg_user_id == tg_user_id)
        if blocked:
            statement = statement.values(
                bot_blocked_at=func.coalesce(models.User.bot_blocked_at, at)
            )
        else:
            statement = statement.where(models.User.bot_blocked_at.is_not(None)).values(
                bot_blocked_at=None
            )
        done = await self._session.execute(statement.returning(models.User.id))
        return done.scalar_one_or_none()

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

    async def claim_paywall_offer(
        self, user_id: int, *, now: datetime, cooldown: timedelta
    ) -> bool:
        """Занять право предложить подписку: не чаще, чем раз в `cooldown`.

        Условный `UPDATE`, а не «прочитал — решил — записал»: двойное нажатие запускает
        два поиска сразу, и проверка без атомарности показала бы предложение обоим.
        «Сейчас» приходит параметром, `now()` базы не используется (как во всей квоте).
        """
        users = cast(Table, models.User.__table__)
        claimed = await self._session.scalar(
            update(users)
            .where(
                users.c.id == user_id,
                or_(
                    users.c.paywall_offered_at.is_(None),
                    users.c.paywall_offered_at <= now - cooldown,
                ),
            )
            .values(paywall_offered_at=now)
            .returning(users.c.id)
        )
        return claimed is not None
