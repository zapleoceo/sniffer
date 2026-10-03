"""Агент слежения на живом Postgres: отбор по свойствам, вердикт, журнал показов, сводка «ещё N».

Пропускаются без `TEST_DATABASE_URL` (см. `conftest.py`). Держится всё на самой базе: JSONB-условия
по свойствам, `min(id)` с отбором, соединение журнала с периодом, `jsonb_set`, окно ранга слотов.
Тот же проход без базы — `test_monitor_slot.py`.

Время — настоящее «сейчас» плюс смещения (правило `test_db_clock_rule.py`): `extracted_at`
пишет база своими часами, и зашитая дата сравнивалась бы с ними вслепую.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db import models
from sniffer.db.repositories import (
    ListingRepository,
    PassportRepository,
    RawMessageRepository,
    UserRepository,
)
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.db.repositories.monitors import MonitorRepository
from sniffer.db.repositories.quota import QuotaRepository
from sniffer.domain.monitoring import OVERFLOW_KIND, Overflow
from sniffer.domain.passport import Category, Intent, Passport
from sniffer.domain.quota import Channel, Claim
from sniffer.domain.records import Listing, RawMessage
from sniffer.matching import filter_for

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL не задан: живого Postgres нет",
)


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _passport(**overrides: Any) -> Passport:
    fields: dict[str, Any] = {
        "intent": Intent.BUY,
        "category": Category.MOTORBIKE,
        "city": "nha_trang",
        "raw_query": "ищу скутер",
    }
    fields.update(overrides)
    return Passport(**fields)


async def _slot(
    session: AsyncSession,
    tg_id: int,
    *,
    expires_at: datetime | None = None,
    root: int | None = None,
) -> tuple[int, int]:
    """Клиент и его слот. Возврат: (user_id, subscription_id)."""
    user = await UserRepository(session).get_or_create(tg_id)
    assert user.id is not None
    stored = await PassportRepository(session).save_new(user.id, _passport())
    row = models.Subscription(
        user_id=user.id, passport_root=root or stored.id, expires_at=expires_at
    )
    session.add(row)
    await session.flush()
    return user.id, row.id


async def _card(session: AsyncSession, number: int, **attributes: Any) -> Listing:
    posted = _now()
    (raw_id,) = await RawMessageRepository(session).add_many(
        [
            RawMessage(
                chat_tg_id=-100777,
                msg_id=number,
                text=f"Продам байк, сообщение {number}",
                text_hash=f"agent-{number}",
                posted_at=posted,
            )
        ]
    )
    return await ListingRepository(session).add(
        Listing(
            raw_message_id=raw_id,
            deal_type="sell",
            category="motorbike",
            city="nha_trang",
            title=f"Байк {number}",
            summary="Автомат",
            tg_link=f"https://t.me/c/1/{number}",
            posted_at=posted,
            attributes=attributes,
        )
    )


# ── отбор по свойствам паспорта: те же критерии, что у диалога (D3) ───────────────────────


async def test_the_monitor_query_applies_the_petrol_default(db_session: AsyncSession) -> None:
    electric = await _card(db_session, 9101, power="electric")
    petrol = await _card(db_session, 9102, power="fuel")
    unknown = await _card(db_session, 9103)
    await db_session.commit()
    spec = filter_for(_passport(), now=_now())
    assert spec is not None

    found = await ListingRepository(db_session).match(spec, after_id=0, limit=500)

    ids = {item.id for item in found}
    assert electric.id not in ids, "электробайк не уходит подписчику ДВС"
    assert {petrol.id, unknown.id} <= ids, "известное совпавшее и неизвестное остаются"


async def test_the_monitor_query_applies_the_volume_band(db_session: AsyncSession) -> None:
    inside = await _card(db_session, 9111, engine_cc=175)
    outside = await _card(db_session, 9112, engine_cc=700)
    await db_session.commit()
    spec = filter_for(_passport(attributes={"engine_cc": 200}), now=_now())
    assert spec is not None

    ids = {i.id for i in await ListingRepository(db_session).match(spec, after_id=0, limit=500)}

    assert inside.id in ids and outside.id not in ids


async def test_the_query_stops_strictly_before_the_given_id(db_session: AsyncSession) -> None:
    first = await _card(db_session, 9121)
    second = await _card(db_session, 9122)
    await db_session.commit()
    assert first.id is not None and second.id is not None
    spec = filter_for(_passport(), now=_now())
    assert spec is not None

    found = await ListingRepository(db_session).match(
        spec, after_id=first.id - 1, before_id=second.id, limit=500
    )

    assert first.id in {item.id for item in found}
    assert second.id not in {item.id for item in found}


# ── вердикт ИИ-проверки (D9) ──────────────────────────────────────────────────────────────


async def test_a_card_without_a_verdict_blocks_until_it_is_screened_or_expires(
    db_session: AsyncSession,
) -> None:
    card = await _card(db_session, 9131)
    await db_session.commit()
    assert card.id is not None
    spec = filter_for(_passport(), now=_now())
    assert spec is not None
    repo = ListingRepository(db_session)
    fresh_cutoff = _now() - timedelta(minutes=5)

    assert await repo.first_unready_id(spec, after_id=card.id - 1, verdict_before=fresh_cutoff) == (
        card.id
    ), "только что извлечённая и непроверенная карточка ждёт вердикта"
    waited = _now() + timedelta(hours=1)
    assert await repo.first_unready_id(spec, after_id=card.id - 1, verdict_before=waited) is None, (
        "срок ожидания вышел — карточке доверяем без вердикта"
    )

    await db_session.execute(
        update(models.Listing).where(models.Listing.id == card.id).values(screened_at=_now())
    )
    await db_session.commit()
    assert (
        await repo.first_unready_id(spec, after_id=card.id - 1, verdict_before=fresh_cutoff) is None
    ), "вердикт есть"


async def test_a_card_from_a_board_never_waits_for_the_model(db_session: AsyncSession) -> None:
    card = await _card(db_session, 9141)
    assert card.id is not None
    await db_session.execute(
        update(models.Listing).where(models.Listing.id == card.id).values(source="chotot")
    )
    await db_session.commit()
    spec = filter_for(_passport(), now=_now())
    assert spec is not None

    found = await ListingRepository(db_session).first_unready_id(
        spec, after_id=card.id - 1, verdict_before=_now() - timedelta(minutes=5)
    )

    assert found is None, "Chotot проверкой не читается: ждать нечего"


# ── журнал показов: слежение пишет, не считаясь в потолок ─────────────────────────────────


async def test_seen_knows_what_the_monitor_recorded_and_is_empty_without_a_period(
    db_session: AsyncSession,
) -> None:
    user_id, _ = await _slot(db_session, 9151)
    card = await _card(db_session, 9152)
    other = await _card(db_session, 9153)
    await db_session.commit()
    assert card.id is not None and other.id is not None
    ledger = QuotaRepository(db_session)
    moment = _now()

    assert await ledger.seen(user_id, [card.id], moment) == set(), "периода ещё нет"
    await ledger.reserve(
        Claim(
            user_id=user_id, listing_ids=(card.id,), channel=Channel.MONITOR, now=moment, limit=None
        )
    )
    await db_session.commit()

    assert await ledger.seen(user_id, [card.id, other.id], moment) == {card.id}
    usage = await ledger.usage(user_id, moment)
    assert usage.used == 0, "карточка слежения в потолок периода не входит"


async def test_a_sent_monitor_card_is_confirmed_in_the_ledger(db_session: AsyncSession) -> None:
    user_id, sub_id = await _slot(db_session, 9161)
    card = await _card(db_session, 9162)
    await db_session.commit()
    assert card.id is not None
    moment = _now()
    delivery = DeliveryRepository(db_session)
    await delivery.enqueue(
        subscription_id=sub_id,
        user_id=user_id,
        listing_id=card.id,
        score=0.9,
        payload={"a": 1},
        now=moment,
    )
    await QuotaRepository(db_session).reserve(
        Claim(
            user_id=user_id, listing_ids=(card.id,), channel=Channel.MONITOR, now=moment, limit=None
        )
    )
    await db_session.commit()
    message_id = await db_session.scalar(
        select(models.Outbox.id).where(models.Outbox.subscription_id == sub_id)
    )
    assert message_id is not None
    view = select(models.OfferView.delivered_at).where(models.OfferView.listing_id == card.id)
    assert await db_session.scalar(view) is None, "пока сообщение не ушло, это резерв"

    sent = moment + timedelta(seconds=3)
    await delivery.mark_sent(message_id, now=sent)
    await db_session.commit()

    assert await db_session.scalar(view) == sent


# ── сводка «ещё N» ────────────────────────────────────────────────────────────────────────


async def test_the_pending_summary_is_refined_and_the_sent_one_is_left_alone(
    db_session: AsyncSession,
) -> None:
    user_id, sub_id = await _slot(db_session, 9171)
    await db_session.commit()
    delivery = DeliveryRepository(db_session)
    when = _now() + timedelta(minutes=30)
    await delivery.enqueue_notice(
        subscription_id=sub_id,
        user_id=user_id,
        payload={"kind": OVERFLOW_KIND, "count": 3, "cap": 10},
        scheduled_at=when,
    )
    await db_session.commit()

    assert await delivery.bump_overflow_notice(sub_id, count=8) == 1
    await db_session.commit()
    row = (
        await db_session.scalars(
            select(models.Outbox).where(models.Outbox.subscription_id == sub_id)
        )
    ).one()
    assert row.payload == {"kind": OVERFLOW_KIND, "count": 8, "cap": 10}

    row.status = "sent"
    await db_session.commit()
    assert await delivery.bump_overflow_notice(sub_id, count=99) == 0, "ушедшее не переписывается"


async def test_an_overflow_count_is_stored_and_the_total_accumulates(
    db_session: AsyncSession,
) -> None:
    _, sub_id = await _slot(db_session, 9181)
    await db_session.commit()
    monitors = MonitorRepository(db_session)
    today = date(2026, 10, 3)

    await monitors.record_overflow(sub_id, Overflow(today, 3, True), extra=3)
    await monitors.record_overflow(sub_id, Overflow(today, 7, True), extra=4)
    await db_session.commit()

    row = await db_session.get(models.Subscription, sub_id, populate_existing=True)
    assert row is not None
    assert (row.overflow_day, row.overflow_count, row.overflow_notified) == (today, 7, True)
    assert row.suppressed_total == 7


# ── слоты клиента ─────────────────────────────────────────────────────────────────────────


async def test_ranked_slots_list_only_the_entitled_ones_oldest_first(
    db_session: AsyncSession,
) -> None:
    now = _now()
    user_id, first = await _slot(db_session, 9191)
    second = models.Subscription(user_id=user_id, passport_root=first + 1000)
    expired = models.Subscription(
        user_id=user_id, passport_root=first + 2000, expires_at=now - timedelta(days=1)
    )
    db_session.add_all([second, expired])
    await db_session.flush()
    await db_session.commit()

    ranked = await MonitorRepository(db_session).ranked_slots([user_id], now=now)

    assert ranked == {user_id: [first, second.id]}, "просроченный слот в ранг не входит"


async def test_no_slot_since_is_set_and_cleared(db_session: AsyncSession) -> None:
    _, sub_id = await _slot(db_session, 9195)
    await db_session.commit()
    monitors = MonitorRepository(db_session)
    since = _now()

    await monitors.set_no_slot_since(sub_id, since)
    await db_session.commit()
    row = await db_session.get(models.Subscription, sub_id, populate_existing=True)
    assert row is not None and row.no_slot_since == since

    await monitors.set_no_slot_since(sub_id, None)
    await db_session.commit()
    row = await db_session.get(models.Subscription, sub_id, populate_existing=True)
    assert row is not None and row.no_slot_since is None
