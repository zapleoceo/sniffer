"""Очередь доставки в комнату на живом Postgres: выборка правее курсора и сдвиг курсора.

Пропускается без `TEST_DATABASE_URL` (см. `conftest.py`): держится на join'ах, `COALESCE`
по курсору и `ON CONFLICT ... GREATEST`, а подделка проверяла бы подделку.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db import models
from sniffer.db.repositories import (
    ListingRepository,
    PassportRepository,
    RawMessageRepository,
    UserRepository,
)
from sniffer.db.repositories.room_relay import RoomRelayRepository
from sniffer.domain.passport import Category, Intent, Passport
from sniffer.domain.records import Listing, RawMessage

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL не задан: живого Postgres нет",
)


async def subscription(session: AsyncSession, tg_id: int) -> int:
    user = await UserRepository(session).get_or_create(tg_id)
    assert user.id is not None
    passport = Passport(
        intent=Intent.RENT, category=Category.APARTMENT, city="nha_trang", raw_query="ищу жильё"
    )
    stored = await PassportRepository(session).save_new(user.id, passport)
    row = models.Subscription(user_id=user.id, passport_root=stored.id)
    session.add(row)
    await session.flush()
    return row.id


async def notify(session: AsyncSession, sub_id: int, number: int, *, text: bool = True) -> int:
    posted = datetime.now(UTC) - timedelta(days=2)
    (raw_id,) = await RawMessageRepository(session).add_many(
        [
            RawMessage(
                chat_tg_id=-100777,
                msg_id=number,
                text=f"Сдам студию {number}",
                text_hash=f"relay-{number}",
                posted_at=posted,
            )
        ]
    )
    card = await ListingRepository(session).add(
        Listing(
            raw_message_id=raw_id if text else None,
            deal_type="rent",
            category="apartment",
            city="nha_trang",
            title=f"Студия {number}",
            summary="Студия",
            tg_link=f"https://t.me/c/7/{number}",
            posted_at=posted,
            attributes={"kitchen": "separate"},
        )
    )
    session.add(models.ListingMedia(listing_id=card.id, r2_key=f"k/{number}"))
    note = models.Notification(subscription_id=sub_id, listing_id=card.id, score=0.8)
    session.add(note)
    await session.flush()
    return note.id


async def test_pending_returns_only_watched_subscriptions_in_id_order(
    db_session: AsyncSession,
) -> None:
    watched = await subscription(db_session, 9001)
    other = await subscription(db_session, 9002)
    first = await notify(db_session, watched, 1)
    await notify(db_session, other, 2)
    second = await notify(db_session, watched, 3)

    found = await RoomRelayRepository(db_session).pending([watched], 10)

    assert [c.notification_id for c in found] == [first, second]
    head = found[0]
    assert head.tg_link == "https://t.me/c/7/1"
    assert head.text == "Сдам студию 1"
    assert head.attributes == {"kitchen": "separate"}
    assert (head.media_count, head.has_media) == (1, True)


async def test_pending_without_subscriptions_reads_nothing(db_session: AsyncSession) -> None:
    assert await RoomRelayRepository(db_session).pending([], 10) == []


async def test_a_listing_without_raw_message_still_comes_with_no_text(
    db_session: AsyncSession,
) -> None:
    sub = await subscription(db_session, 9003)
    await notify(db_session, sub, 4, text=False)
    (item,) = await RoomRelayRepository(db_session).pending([sub], 10)
    assert item.text is None


async def test_the_cursor_moves_forward_only_and_hides_what_was_delivered(
    db_session: AsyncSession,
) -> None:
    sub = await subscription(db_session, 9004)
    ids = [await notify(db_session, sub, n) for n in (5, 6, 7)]
    repo = RoomRelayRepository(db_session)

    await repo.advance(sub, ids[1])
    assert [c.notification_id for c in await repo.pending([sub], 10)] == [ids[2]]

    await repo.advance(sub, ids[0])  # назад курсор не ходит
    stored = await db_session.scalar(
        select(models.RoomRelayCursor.last_notification_id).where(
            models.RoomRelayCursor.subscription_id == sub
        )
    )
    assert stored == ids[1]


async def test_the_limit_caps_the_batch(db_session: AsyncSession) -> None:
    sub = await subscription(db_session, 9005)
    ids = [await notify(db_session, sub, n) for n in (8, 9, 10)]
    found = await RoomRelayRepository(db_session).pending([sub], 2)
    assert [c.notification_id for c in found] == ids[:2]
