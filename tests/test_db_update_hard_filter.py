"""CAS-замена hard_filter на настоящем PostgreSQL: меняется только фильтр."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db import models
from sniffer.db.repositories.slots import SlotRepository
from sniffer.domain.hard_filter import BALCONY, HardFilter
from tests.test_db_grant_watch import _client

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL не задан: живого Postgres нет",
)


async def _snapshot(session: AsyncSession, subscription_id: int) -> dict[str, object]:
    row = (
        await session.execute(
            select(models.Subscription)
            .where(models.Subscription.id == subscription_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    data = {c.name: getattr(row, c.name) for c in models.Subscription.__table__.columns}
    data.pop("hard_filter")
    return data


async def test_update_hard_filter_is_a_cas_that_changes_nothing_but_the_filter(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    user_id, root = await _client(db_session, 770_010)
    other_user, other_root = await _client(db_session, 770_011)
    slots = SlotRepository(db_session)
    wide = HardFilter(
        require=frozenset({BALCONY}), districts=frozenset({"vinh_hoa"}), place_words=("duong de",)
    )
    sub = await slots.grant(user_id, root, hard_filter=wide, lookback=timedelta(0), now=now)
    other = await slots.grant(
        other_user, other_root, hard_filter=wide, lookback=timedelta(0), now=now
    )
    await db_session.execute(
        update(models.Subscription)
        .where(models.Subscription.id == sub)
        .values(since_listing_id=11, scan_listing_id=22, no_slot_since=now, quarantined_until=now)
    )
    await db_session.flush()
    before = await _snapshot(db_session, sub)
    other_before = await _snapshot(db_session, other)
    notifications = (await db_session.scalars(select(models.Notification))).all()
    new = HardFilter(
        require=wide.require,
        districts=wide.districts,
        place_words=wide.place_words,
        allow_unknown=frozenset({BALCONY}),
    )

    conflict = await slots.update_hard_filter(
        sub, expected=HardFilter(require=frozenset({BALCONY})), new=new
    )
    assert not conflict.applied and conflict.current == wide.to_json()
    row = await db_session.get(models.Subscription, sub, populate_existing=True)
    assert row is not None and row.hard_filter == wide.to_json()

    done = await slots.update_hard_filter(sub, expected=wide, new=new)
    assert done.applied
    row = await db_session.get(models.Subscription, sub, populate_existing=True)
    assert row is not None and row.hard_filter == new.to_json()
    assert {"require", "districts", "place_words"} <= set(row.hard_filter or {})
    assert await _snapshot(db_session, sub) == before, "кроме hard_filter ничего не изменилось"
    assert await _snapshot(db_session, other) == other_before
    other_row = await db_session.get(models.Subscription, other, populate_existing=True)
    assert other_row is not None and other_row.hard_filter == wide.to_json()
    assert (await db_session.scalars(select(models.Notification))).all() == notifications

    with pytest.raises(LookupError):
        await slots.update_hard_filter(10**9, expected=None, new=new)
