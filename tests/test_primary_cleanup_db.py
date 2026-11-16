"""Уборка первоисточников на живом Postgres (без `TEST_DATABASE_URL` пропускаются)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db import models
from sniffer.db.repositories.listings import ListingRepository
from sniffer.db.repositories.primary_cleanup import PrimaryCleanupRepository
from sniffer.db.repositories.raw_messages import RawMessageRepository
from sniffer.domain.records import Listing
from tests.test_primary_sources_db import TEXT, chats, raw

NOW = datetime.now(UTC).replace(microsecond=0)


async def test_cleanup_merges_once_and_keeps_history(db_session: AsyncSession) -> None:
    await chats(db_session)
    first_id, repost_id = await RawMessageRepository(db_session).add_many(
        [raw(-1001, 1, 48), raw(-1002, 7, 1)]
    )
    listings = ListingRepository(db_session)
    keep = await listings.add(card(first_id, "-1001:1", 48, 6_000_000))
    gone = await listings.add(card(repost_id, "-1002:7", 1, 5_500_000))
    await db_session.commit()
    assert keep.id is not None and gone.id is not None
    repo = PrimaryCleanupRepository(db_session)

    assert await repo.merge(keep.id, (gone.id,), gone.id, NOW - timedelta(hours=1))
    await db_session.commit()
    # Повтор ничего не делает: дубль уже погашен, событие второй раз не пишется.
    assert not await repo.merge(keep.id, (gone.id,), gone.id, NOW - timedelta(hours=1))
    await db_session.commit()

    kept = await listings.get(keep.id)
    merged = await listings.get(gone.id)
    assert kept is not None and kept.is_active and kept.tg_link == "https://t.me/a_group/1"
    assert kept.price_amount == Decimal(5_500_000)
    assert merged is not None and not merged.is_active
    events = await db_session.scalar(select(func.count()).select_from(models.ListingCleanupEvent))
    assert events == 2  # снимок первоисточника и погашенного дубля, по разу


async def test_cleanup_moves_a_card_to_the_earlier_copy(db_session: AsyncSession) -> None:
    await chats(db_session)
    _, late_id = await RawMessageRepository(db_session).add_many(
        [raw(-1001, 1, 48), raw(-1002, 7, 1)]
    )
    listings = ListingRepository(db_session)
    bound = await listings.add(card(late_id, "-1002:7", 1, 6_000_000))
    await db_session.commit()
    repo = PrimaryCleanupRepository(db_session)

    (row,) = await repo.earlier_copies()
    assert not row["blocked"] and not row["same_chat"]
    assert await repo.rebind(row, "https://t.me/a_group/1")
    await db_session.commit()
    assert not await repo.rebind(row, "https://t.me/a_group/1")  # повтор — пусто

    moved = await listings.get(bound.id or 0)
    assert moved is not None and moved.tg_link == "https://t.me/a_group/1"
    assert moved.external_id == "-1001:1"
    assert await repo.earlier_copies() == []


def card(raw_id: int, external_id: str, hours: int, price: int) -> Listing:
    chat, msg = external_id.split(":")
    group = {"-1001": "a_group", "-1002": "b_group"}[chat]
    return Listing(
        raw_message_id=raw_id,
        deal_type="rent_out",
        category="apartment",
        city="nha_trang",
        title="Студия",
        summary=TEXT,
        tg_link=f"https://t.me/{group}/{msg}",
        posted_at=NOW - timedelta(hours=hours),
        price_amount=Decimal(price),
        price_currency="VND",
        price_period="month",
        external_id=external_id,
    )


async def test_merge_snapshot_restores_prices_attributes_and_deactivation(
    db_session: AsyncSession,
) -> None:
    from dataclasses import replace

    from sqlalchemy import update

    await chats(db_session)
    first_id, repost_id = await RawMessageRepository(db_session).add_many(
        [raw(-1001, 1, 48), raw(-1002, 7, 1)]
    )
    listings = ListingRepository(db_session)
    attrs = {"rooms": 1, "rate_amount": 6_000_000, "nested": {"original": True}}
    keep = await listings.add(replace(card(first_id, "-1001:1", 48, 6_000_000), attributes=attrs))
    gone = await listings.add(
        replace(card(repost_id, "-1002:7", 1, 5_500_000), attributes={"rate_amount": 5_500_000})
    )
    assert keep.id is not None and gone.id is not None
    await db_session.commit()
    assert await PrimaryCleanupRepository(db_session).merge(
        keep.id, (gone.id,), gone.id, NOW - timedelta(hours=1)
    )
    await db_session.commit()
    db_session.expire_all()
    changed = await listings.get(keep.id)
    assert changed is not None and changed.attributes["rate_amount"] == 5_500_000
    events = (await db_session.scalars(select(models.ListingCleanupEvent))).all()
    assert len(events) == 2
    for event in events:
        before = event.before
        assert set(models.Listing.__table__.columns.keys()) <= before.keys()
        values = {
            key: before[key]
            for key in (
                "attributes",
                "price_currency",
                "price_period",
                "is_active",
                "deactivated_reason",
                "tg_link",
            )
        }
        for key in ("posted_at", "deactivated_at"):
            values[key] = datetime.fromisoformat(before[key]) if before[key] else None
        for key in ("price_amount", "price_usd_month"):
            values[key] = Decimal(str(before[key])) if before[key] is not None else None
        await db_session.execute(
            update(models.Listing).where(models.Listing.id == event.listing_id).values(**values)
        )
    await db_session.commit()
    db_session.expire_all()
    restored = await listings.get(keep.id)
    restored_duplicate = await listings.get(gone.id)
    assert restored is not None and restored.attributes == attrs
    assert restored.price_amount == keep.price_amount and restored.posted_at == keep.posted_at
    assert restored_duplicate is not None and restored_duplicate.is_active
    row = await db_session.get(models.Listing, gone.id)
    assert row is not None and row.deactivated_at is None and row.deactivated_reason is None
