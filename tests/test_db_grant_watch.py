"""Ручная выдача слежения на живом Postgres: строка без срока, фильтр доезжает до монитора.

Пропускается без `TEST_DATABASE_URL` (см. `conftest.py`). Время — настоящее «сейчас»
(правило `test_db_clock_rule.py`).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db import models
from sniffer.db.repositories import PassportRepository, UserRepository
from sniffer.db.repositories.monitors import MonitorRepository
from sniffer.db.repositories.slots import SlotRepository
from sniffer.domain.hard_filter import BALCONY, HardFilter
from sniffer.domain.passport import Category, Intent, Passport

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL не задан: живого Postgres нет",
)

SPEC = HardFilter(require=frozenset({BALCONY}), districts=frozenset({"vinh_hoa"}))


async def _client(session: AsyncSession, tg_id: int) -> tuple[int, int]:
    user = await UserRepository(session).get_or_create(tg_id)
    assert user.id is not None
    passport = Passport(
        intent=Intent.RENT, category=Category.APARTMENT, city="nha_trang", raw_query="квартира"
    )
    stored = await PassportRepository(session).save_new(user.id, passport)
    return user.id, stored.id


async def test_grant_creates_an_unmetered_monitor_that_the_agent_reads_with_its_filter(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    user_id, root = await _client(db_session, 770_001)

    subscription_id = await SlotRepository(db_session).grant(
        user_id, root, hard_filter=SPEC, lookback=timedelta(0), now=now
    )
    await db_session.flush()

    row = await db_session.get(models.Subscription, subscription_id)
    assert row is not None and row.expires_at is None and row.is_active
    due = await MonitorRepository(db_session).claim_due(limit=500, now=now)
    mine = [item for item in due.ready if item.id == subscription_id]
    assert [item.hard_filter for item in mine] == [SPEC]


async def test_grant_twice_updates_one_row_and_a_sync_does_not_expire_it(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    user_id, root = await _client(db_session, 770_002)
    slots = SlotRepository(db_session)

    first = await slots.grant(user_id, root, hard_filter=None, lookback=timedelta(0), now=now)
    await db_session.execute(
        update(models.Subscription).where(models.Subscription.id == first).values(is_active=False)
    )
    second = await slots.grant(user_id, root, hard_filter=SPEC, lookback=timedelta(0), now=now)
    await slots.sync(user_id, now)
    await db_session.flush()

    assert first == second
    rows = (
        await db_session.scalars(
            select(models.Subscription)
            .where(models.Subscription.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].expires_at is None and rows[0].is_active
    assert rows[0].hard_filter == SPEC.to_json()
