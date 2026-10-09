"""Слоты мониторинга в базе: пересчёт сроков по платежам, включение, перенос.

Арифметика — в `domain/slots.py` (чистая, проверяется без базы); здесь только чтение
журнала, запись результата и замок на клиента. Замок — `SELECT … FOR UPDATE` строки
клиента: два события одного клиента (платёж и нажатие «Следить» в одну секунду) не
пересчитают слоты вперемешку. Пересчёт идемпотентен: повтор с теми же данными ничего
не меняет, поэтому его можно звать из любого места, где менялись деньги или порядок.

Ничего не удаляется. Мониторинг без слота — строка с просроченным `expires_at`: фильтр,
курсор и история целы, а право (`delivery.entitled`) его читает как «слота нет».
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select, update

from sniffer.db import models
from sniffer.db.repositories.base import Repository
from sniffer.db.repositories.billing import BillingRepository
from sniffer.domain.hard_filter import HardFilter
from sniffer.domain.slots import (
    Monitor,
    Outcome,
    SlotState,
    decide_enable,
    plan_move,
    state_after,
)


class SlotRepository(Repository):
    async def monitors(self, user_id: int) -> list[Monitor]:
        rows = await self._session.execute(
            select(models.Subscription)
            .where(models.Subscription.user_id == user_id)
            # Строки уже могли лежать в сессии до сдвига порядка или срока: читаем базу.
            .execution_options(populate_existing=True)
        )
        return [
            Monitor(
                id=row.id,
                root=row.passport_root,
                priority=row.priority,
                expires_at=row.expires_at,
                is_active=row.is_active,
            )
            for row in rows.scalars()
        ]

    async def sync(self, user_id: int, now: datetime) -> SlotState:
        """Разложить оплаченные сроки по мониторингам клиента и вернуть итог."""
        await self._lock(user_id)
        return await self._sync_locked(user_id, now)

    async def enable(self, user_id: int, root: int, now: datetime) -> tuple[Outcome, SlotState]:
        """«Следить» на ветке: заводит мониторинг или возобновляет, если слот есть."""
        await self._lock(user_id)
        ends = await BillingRepository(self._session).live_period_ends(user_id, now)
        monitors = await self.monitors(user_id)
        outcome = decide_enable(len(ends), monitors, root, now)
        if outcome is Outcome.ENABLE:
            existing = next((monitor for monitor in monitors if monitor.root == root), None)
            if existing is None:
                top = max((monitor.priority for monitor in monitors), default=-1)
                await self._create(user_id, root, priority=top + 1, now=now)
            else:
                await self._resume(existing.id)
        return outcome, await self._sync_locked(user_id, now)

    async def grant(
        self,
        user_id: int,
        root: int,
        *,
        hard_filter: HardFilter | None,
        lookback: timedelta,
        now: datetime,
    ) -> int:
        """Мониторинг без платежа и без срока (`expires_at IS NULL`): ручная выдача владельцем.

        Единственный путь к такой строке в коде: платёж срок ставит всегда, «Следить» без
        оплаченного слота не включается. Строка не занимает оплаченных слотов и пересчётом
        сроков не трогается (`assign_expiry` пропускает `expires_at IS NULL`). Курсор встаёт на
        последнюю карточку, опубликованную ДО `now - lookback`: окно «последние N часов» монитор
        просмотрит на первом же проходе, остальное придёт как новое. Нулевое окно — только
        новое. Возвращает id мониторинга.
        """
        await self._lock(user_id)
        cursor_query = select(func.coalesce(func.max(models.Listing.id), 0))
        if lookback > timedelta(0):
            cursor_query = cursor_query.where(models.Listing.posted_at <= now - lookback)
        cursor = int(await self._session.scalar(cursor_query) or 0)
        monitors = await self.monitors(user_id)
        existing = next((monitor for monitor in monitors if monitor.root == root), None)
        stored = hard_filter.to_json() if hard_filter is not None else None
        if existing is not None:
            await self._session.execute(
                update(models.Subscription)
                .where(models.Subscription.id == existing.id)
                .values(is_active=True, expires_at=None, hard_filter=stored, no_slot_since=None)
            )
            return existing.id
        row = models.Subscription(
            user_id=user_id,
            passport_root=root,
            is_active=True,
            priority=max((monitor.priority for monitor in monitors), default=-1) + 1,
            expires_at=None,
            hard_filter=stored,
            since_listing_id=cursor,
            scan_listing_id=cursor,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def move(self, user_id: int, *, to_root: int, from_root: int, now: datetime) -> bool:
        """Слот с `from_root` на `to_root`: порядок меняется, мониторинги остаются."""
        await self._lock(user_id)
        plan = plan_move(
            await self.monitors(user_id), to_root=to_root, from_root=from_root, now=now
        )
        if plan is None:
            return False
        await self._set_priority(plan.demote_id, plan.demote_to)
        if plan.promote_id is None:
            await self._create(user_id, to_root, priority=plan.promote_to, now=now)
        else:
            await self._set_priority(plan.promote_id, plan.promote_to)
            await self._resume(plan.promote_id)
        await self._sync_locked(user_id, now)
        return True

    async def _sync_locked(self, user_id: int, now: datetime) -> SlotState:
        ends = await BillingRepository(self._session).live_period_ends(user_id, now)
        state, changes = state_after(ends, await self.monitors(user_id), now)
        for monitor_id, expires_at in changes.items():
            await self._session.execute(
                update(models.Subscription)
                .where(models.Subscription.id == monitor_id)
                .values(expires_at=expires_at)
            )
        return state

    async def _lock(self, user_id: int) -> None:
        await self._session.execute(
            select(models.User.id).where(models.User.id == user_id).with_for_update()
        )

    async def _create(self, user_id: int, root: int, *, priority: int, now: datetime) -> None:
        """Новый мониторинг: слежение начинается с «сейчас», а не с пересказа прошлого."""
        newest = await self._session.scalar(select(func.coalesce(func.max(models.Listing.id), 0)))
        self._session.add(
            models.Subscription(
                user_id=user_id,
                passport_root=root,
                is_active=True,
                priority=priority,
                # Срок «сейчас» = слота ещё нет; настоящий срок поставит пересчёт сразу после.
                expires_at=now,
                since_listing_id=newest or 0,
                scan_listing_id=newest or 0,
            )
        )
        await self._session.flush()

    async def _set_priority(self, monitor_id: int, priority: int) -> None:
        await self._session.execute(
            update(models.Subscription)
            .where(models.Subscription.id == monitor_id)
            .values(priority=priority)
        )

    async def _resume(self, monitor_id: int) -> None:
        await self._session.execute(
            update(models.Subscription)
            .where(models.Subscription.id == monitor_id)
            .values(is_active=True)
        )
