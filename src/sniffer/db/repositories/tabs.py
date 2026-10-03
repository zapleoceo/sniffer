"""Вкладки поиска: связь «тема Telegram ↔ корень поиска».

Тема — представление поиска, а не его замена: источник истины остаётся в цепочке версий, а
эта таблица только говорит, в какой теме поиск показан. Удаление темы человеком не удаляет
ни поиск, ни подписку: Telegram о нём не сообщает, поэтому отказ отправки в тему переводит
связь в `lost` (`mark_lost`), а доставка идёт в General.

Все записи идемпотентны (`ON CONFLICT`): два первых сообщения в новой теме приходят
параллельными апдейтами, и проигравший гонки не должен ни упасть, ни завести вторую ветку.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from sqlalchemy import Table, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sniffer.db import models
from sniffer.db.models import tabs
from sniffer.db.repositories.base import Repository


class TabRepository(Repository):
    async def root_of(self, user_id: int, thread_id: int) -> int | None:
        """Корень поиска, живущего в теме. Архивные и утраченные связи темы не обслуживают."""
        root: int | None = await self._session.scalar(
            select(models.SearchTab.passport_root).where(
                models.SearchTab.user_id == user_id,
                models.SearchTab.message_thread_id == thread_id,
                models.SearchTab.state == tabs.OPEN,
            )
        )
        return root

    async def claim(
        self, user_id: int, root: int, thread_id: int, title: str | None = None
    ) -> bool:
        """Привязать новый поиск к теме. `False` — тему или поиск уже заняла другая вставка."""
        table = cast(Table, models.SearchTab.__table__)
        claimed = await self._session.execute(
            pg_insert(table)
            .values(
                user_id=user_id,
                passport_root=root,
                message_thread_id=thread_id,
                shown_title=title,
                state=tabs.OPEN,
            )
            .on_conflict_do_nothing()
            .returning(table.c.id)
        )
        return claimed.scalar_one_or_none() is not None

    async def attach(self, user_id: int, root: int, thread_id: int, title: str) -> bool:
        """Тема для УЖЕ существующего поиска (панель, пересоздание утраченной).

        Убранный (`archived`) поиск тему не получает: пометка «убран» сильнее. `False` — связь
        не изменена.
        """
        table = cast(Table, models.SearchTab.__table__)
        attached = await self._session.execute(
            pg_insert(table)
            .values(
                user_id=user_id,
                passport_root=root,
                message_thread_id=thread_id,
                shown_title=title,
                state=tabs.OPEN,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "passport_root"],
                set_={"message_thread_id": thread_id, "shown_title": title, "state": tabs.OPEN},
                where=table.c.state != tabs.ARCHIVED,
            )
            .returning(table.c.id)
        )
        return attached.scalar_one_or_none() is not None

    async def tab_of(self, user_id: int, root: int) -> tuple[int | None, str | None, str] | None:
        """`(тема, показанное имя, состояние)` или `None`, если связи нет."""
        row = (
            await self._session.execute(
                select(
                    models.SearchTab.message_thread_id,
                    models.SearchTab.shown_title,
                    models.SearchTab.state,
                ).where(models.SearchTab.user_id == user_id, models.SearchTab.passport_root == root)
            )
        ).first()
        return None if row is None else (row[0], row[1], row[2])

    async def set_title(self, user_id: int, root: int, title: str) -> None:
        await self._session.execute(
            update(models.SearchTab)
            .where(models.SearchTab.user_id == user_id, models.SearchTab.passport_root == root)
            .values(shown_title=title)
        )

    async def has_topics(self, user_id: int) -> bool:
        """Человек уже работал в темах: липкий признак «режим тем» (R6 §2.2, слой 4)."""
        return bool(
            await self._session.scalar(
                select(func.count())
                .select_from(models.SearchTab)
                .where(
                    models.SearchTab.user_id == user_id,
                    models.SearchTab.message_thread_id.is_not(None),
                )
            )
        )

    async def threads_for(self, subscription_ids: Sequence[int]) -> dict[int, int]:
        """Живые темы подписок: `{подписка: тема}`. Нет связи или она утрачена — нет ключа."""
        if not subscription_ids:
            return {}
        rows = await self._session.execute(
            select(models.Subscription.id, models.SearchTab.message_thread_id)
            .join(
                models.SearchTab,
                (models.SearchTab.user_id == models.Subscription.user_id)
                & (models.SearchTab.passport_root == models.Subscription.passport_root),
            )
            .where(
                models.Subscription.id.in_(list(subscription_ids)),
                models.SearchTab.state == tabs.OPEN,
                models.SearchTab.message_thread_id.is_not(None),
            )
        )
        return {sub: thread for sub, thread in rows if thread is not None}

    async def mark_lost(self, subscription_id: int) -> bool:
        """Telegram не нашёл тему: связь утрачена, при следующем обращении тему пересоздадут."""
        subscription = (
            select(models.Subscription.user_id, models.Subscription.passport_root)
            .where(models.Subscription.id == subscription_id)
            .subquery()
        )
        lost = await self._session.execute(
            update(models.SearchTab)
            .where(
                models.SearchTab.user_id == subscription.c.user_id,
                models.SearchTab.passport_root == subscription.c.passport_root,
                models.SearchTab.state == tabs.OPEN,
            )
            .values(state=tabs.LOST)
            .returning(models.SearchTab.id)
        )
        return lost.scalar_one_or_none() is not None
