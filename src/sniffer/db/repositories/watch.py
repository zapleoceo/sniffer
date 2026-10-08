"""Слежения клиента: занятые слоты, «Удалить поиск» (пауза и архив), число поисков.

Слот — оплаченная подписка Stars; её привязка к поиску — строка `subscriptions` (корень
цепочки). Переносит и пересчитывает слоты `SlotRepository`: у слотов один владелец, и
второго пути переноса здесь нет.

Архив поиска — строка `search_tabs` в состоянии `archived`. Версии паспорта не удаляются:
цепочка нужна, чтобы объяснить прошлую выдачу, а слежение уходит на паузу, но слот остаётся
за клиентом до конца оплаченного срока и доступен к переносу.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sqlalchemy import Table, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sniffer.db import models
from sniffer.db.models import tabs
from sniffer.db.repositories.base import Repository
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.db.repositories.passports import not_archived


@dataclass(frozen=True, slots=True)
class SlotRow:
    root: int
    is_active: bool
    expires_at: datetime
    archived: bool


class WatchRepository(Repository):
    async def live_slots(self, user_id: int, now: datetime) -> list[SlotRow]:
        """Привязанные слоты: оплаченные на момент `now`, включая те, что на паузе."""
        archived = (
            select(models.SearchTab.id)
            .where(
                models.SearchTab.user_id == models.Subscription.user_id,
                models.SearchTab.passport_root == models.Subscription.passport_root,
                models.SearchTab.state == tabs.ARCHIVED,
            )
            .exists()
        )
        rows = await self._session.execute(
            select(
                models.Subscription.passport_root,
                models.Subscription.is_active,
                models.Subscription.expires_at,
                archived,
            )
            .where(models.Subscription.user_id == user_id, models.Subscription.expires_at > now)
            .order_by(models.Subscription.id)
        )
        return [SlotRow(root, active, expires, bool(gone)) for root, active, expires, gone in rows]

    async def count_searches(self, user_id: int) -> int:
        """Сколько поисков не убрано: ровно то, что считается пределом «1 / 10»."""
        chain = func.coalesce(models.Passport.root_id, models.Passport.id)
        return int(
            await self._session.scalar(
                select(func.count(func.distinct(chain))).where(
                    models.Passport.user_id == user_id, not_archived()
                )
            )
            or 0
        )

    async def archive(self, user_id: int, root: int) -> bool:
        """«Удалить поиск»: пауза слежения, пометка «архив», сброс указателей клиента.

        Идемпотентно: повтор по старой кнопке ничего не ломает. `False` — поиск не клиента.
        """
        if not await self._owns(user_id, root):
            return False
        await self._session.execute(
            update(models.Subscription)
            .where(
                models.Subscription.user_id == user_id, models.Subscription.passport_root == root
            )
            .values(is_active=False)
        )
        await DeliveryRepository(self._session).cancel_pending_for_search(user_id, root)
        table = cast(Table, models.SearchTab.__table__)
        await self._session.execute(
            pg_insert(table)
            .values(user_id=user_id, passport_root=root, state=tabs.ARCHIVED)
            .on_conflict_do_update(
                index_elements=["user_id", "passport_root"], set_={"state": tabs.ARCHIVED}
            )
        )
        await self._session.execute(
            update(models.User)
            .where(models.User.id == user_id, models.User.active_passport_root == root)
            .values(active_passport_root=None, editing_passport_root=None)
        )
        return True

    async def _owns(self, user_id: int, root: int) -> bool:
        chain = func.coalesce(models.Passport.root_id, models.Passport.id)
        return bool(
            await self._session.scalar(
                select(models.Passport.id)
                .where(models.Passport.user_id == user_id, chain == root)
                .limit(1)
            )
        )
