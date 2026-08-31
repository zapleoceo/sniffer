"""Сводка по проекту для блока общей статистики.

Отдельный класс, а не метод в каждом репозитории: агрегаты здесь читают сразу
несколько агрегатов домена (чаты, сырьё, карточки), и «по одному классу на
агрегат» для них не выполняется ни при каком разбиении. Ни один метод не пишет.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from sniffer.db import models
from sniffer.db.repositories.base import Repository

# Порог «живости» лота. Тот же смысл, что у пометки stale в verifier/liveness:
# объявление старше двух недель обычно уже продано.
FRESH_DAYS = 14


class StatsRepository(Repository):
    async def summary(self) -> dict[str, int]:
        """Числа, которые дашборд показывает в шапке.

        Все — одним запросом на таблицу. Раздельные `count(*)` по одной и той
        же таблице означают лишний полный обход на каждый показатель.
        """
        chats = (
            await self._session.execute(
                select(
                    func.count(),
                    func.count().filter(models.Chat.is_active.is_(True)),
                )
            )
        ).one()

        raw_total = await self._session.scalar(select(func.count()).select_from(models.RawMessage))

        fresh_since = datetime.now(UTC) - timedelta(days=FRESH_DAYS)
        listings = (
            await self._session.execute(
                select(
                    func.count(),
                    func.count().filter(models.Listing.is_active.is_(True)),
                    func.count().filter(models.Listing.posted_at >= fresh_since),
                )
            )
        ).one()

        users = (
            await self._session.execute(
                select(
                    func.count(),
                    func.count().filter(models.User.is_blocked.is_(True)),
                )
            )
        ).one()

        return {
            "chats": int(chats[0]),
            "chats_active": int(chats[1]),
            "raw_messages": int(raw_total or 0),
            "listings": int(listings[0]),
            "listings_active": int(listings[1]),
            "listings_fresh": int(listings[2]),
            "users": int(users[0]),
            "users_blocked": int(users[1]),
        }
