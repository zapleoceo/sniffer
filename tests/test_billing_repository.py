"""Журнал оплаты звёздами на живом Postgres.

Пропускается без `TEST_DATABASE_URL` (см. `conftest.py`): держится всё на
`ON CONFLICT DO NOTHING`, уникальных ключах и часах самой базы, а на подделке
ни того, ни другого нет. «Сейчас» здесь настоящее: срок подписки сверяется с
`now()` базы, и зашитая дата протухла бы сама (`test_db_clock_rule.py`).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sniffer.db import models
from sniffer.db.repositories.billing import BillingRepository
from sniffer.db.repositories.users import UserRepository
from sniffer.domain.billing import (
    FROM_RECONCILE,
    BillingEvent,
    EventKind,
    PaymentKind,
    PaymentRecord,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL не задан: живого Postgres нет",
)

NOW = datetime.now(UTC).replace(microsecond=0)
PAYLOAD = "v2:s:42:2026-10-03:0123456789ab"


def _record(
    charge: str,
    *,
    tg_user_id: int = 42,
    payload: str = PAYLOAD,
    kind: PaymentKind = PaymentKind.FIRST,
    period_end: datetime | None = None,
    **extra: Any,
) -> PaymentRecord:
    return PaymentRecord(
        charge_id=charge,
        tg_user_id=tg_user_id,
        amount=10,
        currency="XTR",
        kind=kind,
        invoice_payload=payload,
        is_recurring=kind is not PaymentKind.UNKNOWN,
        is_first_recurring=kind is PaymentKind.FIRST,
        period_end=NOW + timedelta(days=30) if period_end is None else period_end,
        raw={"telegram_payment_charge_id": charge, "total_amount": 10, **extra},
    )


async def _user(session: AsyncSession, tg_user_id: int = 42) -> int:
    user = await UserRepository(session).get_or_create(tg_user_id, username="платящий")
    assert user.id is not None
    await session.commit()
    return user.id


async def test_a_payment_is_recorded_once_and_the_raw_update_survives(
    db_session: AsyncSession,
) -> None:
    """Повтор апдейта не создаёт второй строки; сырой JSON доезжает целиком."""
    user_id = await _user(db_session)
    repo = BillingRepository(db_session)

    first = await repo.insert_payment(user_id, _record("charge-1", note="как пришло"))
    await db_session.commit()
    second = await repo.insert_payment(user_id, _record("charge-1", note="другое"))
    await db_session.commit()

    assert (first, second) == (True, False)
    stored = await repo.get_payment("charge-1")
    assert stored is not None
    assert (stored.tg_user_id, stored.kind, stored.status) == (42, "first", "paid")
    assert stored.invoice_payload == PAYLOAD
    assert stored.is_first_recurring and stored.is_recurring
    assert stored.period_end == NOW + timedelta(days=30)
    row = await db_session.get(models.Payment, (await _payment_id(db_session, "charge-1")))
    assert row is not None and row.raw is not None
    assert row.raw["note"] == "как пришло", "повторная запись не перетёрла первую"


async def _payment_id(session: AsyncSession, charge: str) -> int:
    found = await session.scalar(
        select(models.Payment.id).where(models.Payment.external_id == charge)
    )
    assert found is not None
    return int(found)


async def test_two_deliveries_of_one_payment_at_once_make_one_row(
    db_engine: AsyncEngine,
) -> None:
    """Гонка двух повторов одного апдейта: ровно одна запись и ровно один «новый».

    `Barrier` обязателен: без него воркеры идут в базу по очереди, гонка не
    наступает, и тест зеленеет на проверке «а нет ли уже такого».
    """
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as setup:
        user_id = await _user(setup)
    gate = asyncio.Barrier(2)

    async def deliver() -> bool:
        async with sessions() as session:
            await gate.wait()
            fresh = await BillingRepository(session).insert_payment(user_id, _record("charge-race"))
            await session.commit()
            return fresh

    results = await asyncio.gather(deliver(), deliver())

    assert sorted(results) == [False, True]


async def test_a_refund_moves_forward_only_and_a_redelivered_payment_does_not_undo_it(
    db_session: AsyncSession,
) -> None:
    user_id = await _user(db_session)
    repo = BillingRepository(db_session)
    await repo.insert_payment(user_id, _record("charge-r"))
    await db_session.commit()

    first = await repo.mark_refunded("charge-r")
    again = await repo.mark_refunded("charge-r")
    unknown = await repo.mark_refunded("нет-такого")
    redelivered = await repo.insert_payment(user_id, _record("charge-r"))
    await db_session.commit()

    assert (first, again, unknown, redelivered) == (True, False, False, False)
    stored = await repo.get_payment("charge-r")
    assert stored is not None and stored.status == "refunded" and stored.refunded_at is not None


async def test_refund_notice_before_payment_prevents_entitlement_in_real_repository(
    db_session: AsyncSession,
) -> None:
    user_id = await _user(db_session)
    repo = BillingRepository(db_session)
    assert not await repo.mark_refunded("late-charge")
    await repo.record_event(
        BillingEvent(EventKind.REFUNDED, 42, {}, charge_id="late-charge", update_id=901)
    )
    await db_session.commit()

    assert await repo.insert_payment(user_id, _record("late-charge"))
    await db_session.commit()
    stored = await repo.get_payment("late-charge")
    assert stored is not None and stored.status == "refunded"
    assert stored.refunded_at is not None
    assert await repo.live_subscriptions(user_id, NOW) == 0
    assert not await repo.insert_payment(user_id, _record("late-charge"))


async def test_concurrent_refund_notice_and_payment_never_leave_paid_row(
    db_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as setup:
        user_id = await _user(setup)
    gate = asyncio.Barrier(2)

    async def payment() -> None:
        async with sessions() as session:
            await gate.wait()
            await BillingRepository(session).insert_payment(user_id, _record("race-refund"))
            await session.commit()

    async def refund() -> None:
        async with sessions() as session:
            await gate.wait()
            repo = BillingRepository(session)
            await repo.record_event(
                BillingEvent(EventKind.REFUNDED, 42, {}, charge_id="race-refund")
            )
            await repo.mark_refunded("race-refund")
            await session.commit()

    await asyncio.gather(payment(), refund())
    async with sessions() as session:
        repo = BillingRepository(session)
        stored = await repo.get_payment("race-refund")
        assert stored is not None and stored.status == "refunded"
        assert await repo.live_subscriptions(user_id, NOW) == 0


async def test_the_first_charge_of_a_subscription_is_its_earliest_payment(
    db_session: AsyncSession,
) -> None:
    user_id = await _user(db_session)
    repo = BillingRepository(db_session)
    await repo.insert_payment(user_id, _record("first-charge"))
    await repo.insert_payment(
        user_id, _record("renewal-charge", kind=PaymentKind.RENEWAL, period_end=NOW)
    )
    await db_session.commit()

    assert await repo.first_charge_of(PAYLOAD) == "first-charge"
    assert await repo.first_charge_of("v2:s:42:2026-10-03:ffffffffffff") is None


async def test_live_subscriptions_are_counted_by_invoice_with_the_term_from_telegram(
    db_session: AsyncSession,
) -> None:
    """Подписка живёт, пока не истёк срок хотя бы у одного не возвращённого платежа."""
    user_id = await _user(db_session)
    other = await _user(db_session, 43)
    repo = BillingRepository(db_session)
    live, ended, refunded, renewed = (f"v2:s:42:2026-10-03:{c * 12}" for c in "abcd")
    await repo.insert_payment(user_id, _record("live", payload=live))
    await repo.insert_payment(
        user_id, _record("ended", payload=ended, period_end=NOW - timedelta(days=1))
    )
    await repo.insert_payment(user_id, _record("refunded", payload=refunded))
    await repo.mark_refunded("refunded")
    await repo.insert_payment(
        user_id, _record("old", payload=renewed, period_end=NOW - timedelta(days=31))
    )
    await repo.insert_payment(user_id, _record("new", payload=renewed, kind=PaymentKind.RENEWAL))
    await repo.insert_payment(user_id, _record("junk", payload="чужое", kind=PaymentKind.UNKNOWN))
    await repo.insert_payment(
        other, _record("theirs", tg_user_id=43, payload="v2:s:43:2026-10-03:" + "e" * 12)
    )
    await db_session.commit()

    assert await repo.live_subscriptions(user_id, NOW) == 2, (
        "живая и продлённая; остальное не в счёт"
    )
    assert await repo.live_subscriptions(other, NOW) == 1


async def test_consent_is_per_version_and_repeating_it_changes_nothing(
    db_session: AsyncSession,
) -> None:
    user_id = await _user(db_session)
    repo = BillingRepository(db_session)

    assert not await repo.has_consent(user_id, "terms", "2026-10-03")
    await repo.record_consent(user_id, "terms", "2026-10-03")
    await repo.record_consent(user_id, "terms", "2026-10-03")
    await db_session.commit()

    assert await repo.has_consent(user_id, "terms", "2026-10-03")
    assert not await repo.has_consent(user_id, "terms", "2026-11-01")
    assert not await repo.has_consent(user_id, "privacy", "2026-10-03")
    assert not await repo.has_consent(await _user(db_session, 99), "terms", "2026-10-03")


async def test_an_event_is_recorded_once_per_update_and_counted_by_window(
    db_session: AsyncSession,
) -> None:
    repo = BillingRepository(db_session)
    canceled = BillingEvent(EventKind.SUB_CANCELED, 42, {"state": "canceled"}, update_id=500)

    first = await repo.record_event(canceled)
    again = await repo.record_event(canceled)
    support = [
        await repo.record_event(BillingEvent(EventKind.SUPPORT, 42, {"text": "где подписка?"}))
        for _ in range(2)
    ]
    db_session.add(
        models.BillingEvent(
            kind=EventKind.SUPPORT.value,
            tg_user_id=42,
            payload={"text": "давно"},
            created_at=datetime.now(UTC) - timedelta(hours=3),
        )
    )
    await db_session.commit()

    assert (first, again) == (True, False)
    assert support == [True, True], "события без update_id не склеиваются между собой"
    assert await repo.events_within(42, EventKind.SUPPORT, timedelta(hours=1)) == 2
    assert await repo.events_within(42, EventKind.SUPPORT, timedelta(hours=6)) == 3
    assert await repo.events_within(42, EventKind.SUB_CANCELED, timedelta(hours=1)) == 1
    assert await repo.events_within(7, EventKind.SUPPORT, timedelta(hours=6)) == 0


async def test_recent_payments_are_the_clients_own_newest_first(db_session: AsyncSession) -> None:
    user_id = await _user(db_session)
    other = await _user(db_session, 43)
    repo = BillingRepository(db_session)
    for number in range(4):
        await repo.insert_payment(user_id, _record(f"mine-{number}"))
    await repo.insert_payment(other, _record("not-mine", tg_user_id=43))
    await db_session.commit()

    recent = await repo.recent_payments(42, limit=3)

    assert [payment.charge_id for payment in recent] == ["mine-3", "mine-2", "mine-1"]


async def test_a_payment_we_decided_to_return_no_longer_holds_a_slot(
    db_session: AsyncSession,
) -> None:
    """`refunding` пишется до вызова Telegram: слот снят уже тогда, а не после ответа."""
    user_id = await _user(db_session)
    repo = BillingRepository(db_session)
    await repo.insert_payment(user_id, _record("held"))
    await db_session.commit()
    assert await repo.live_subscriptions(user_id, NOW) == 1

    planned = await repo.mark_refunding("held")
    again = await repo.mark_refunding("held")
    await db_session.commit()

    assert (planned, again) == (True, False)
    assert await repo.live_subscriptions(user_id, NOW) == 0
    stored = await repo.get_payment("held")
    assert stored is not None and stored.status == "refunding"
    assert await repo.mark_refunded("held") is True, "refunding идёт дальше, в refunded"


async def test_live_terms_come_longest_first_and_use_the_caller_clock(
    db_session: AsyncSession,
) -> None:
    user_id = await _user(db_session)
    repo = BillingRepository(db_session)
    short, long_, gone = (f"v2:s:42:2026-10-03:{c * 12}" for c in "stu")
    await repo.insert_payment(
        user_id, _record("short", payload=short, period_end=NOW + timedelta(days=3))
    )
    await repo.insert_payment(
        user_id, _record("long", payload=long_, period_end=NOW + timedelta(days=20))
    )
    await repo.insert_payment(
        user_id, _record("gone", payload=gone, period_end=NOW - timedelta(days=1))
    )
    await db_session.commit()

    ends = await repo.live_period_ends(user_id, NOW)

    assert ends == [NOW + timedelta(days=20), NOW + timedelta(days=3)]
    later = await repo.live_period_ends(user_id, NOW + timedelta(days=5))
    assert later == [NOW + timedelta(days=20)], "«сейчас» — аргумент, а не часы базы"


async def test_the_first_payment_of_a_subscription_comes_back_with_its_amount(
    db_session: AsyncSession,
) -> None:
    user_id = await _user(db_session)
    repo = BillingRepository(db_session)
    await repo.insert_payment(user_id, _record("first-charge"))
    await repo.insert_payment(user_id, _record("renewal", kind=PaymentKind.RENEWAL))
    await db_session.commit()

    first = await repo.first_payment_of(PAYLOAD)

    assert first is not None and first.charge_id == "first-charge" and first.amount == 10
    assert await repo.first_payment_of("v2:s:42:2026-10-03:ffffffffffff") is None


async def test_unsettled_refunds_are_foreign_paid_rows_and_refunding_ones_only(
    db_session: AsyncSession,
) -> None:
    user_id = await _user(db_session)
    repo = BillingRepository(db_session)
    await repo.insert_payment(user_id, _record("ours"))
    await repo.insert_payment(
        user_id, _record("foreign", payload="чужое", kind=PaymentKind.UNKNOWN)
    )
    await repo.insert_payment(user_id, _record("half", payload="v2:s:42:2026-10-03:" + "h" * 12))
    await repo.mark_refunding("half")
    await repo.insert_payment(user_id, _record("done", payload="v2:s:42:2026-10-03:" + "d" * 12))
    await repo.mark_refunded("done")
    await db_session.commit()

    found = {
        p.charge_id for p in await repo.unsettled_refunds(datetime.now(UTC) + timedelta(hours=1))
    }
    young = await repo.unsettled_refunds(datetime.now(UTC) - timedelta(days=1))

    assert found == {"foreign", "half"}
    assert young == [], "платёж моложе границы сверка не трогает"


async def test_a_reconciled_payment_remembers_where_it_came_from(
    db_session: AsyncSession,
) -> None:
    user_id = await _user(db_session)
    repo = BillingRepository(db_session)
    base = _record("found-later")
    record = PaymentRecord(
        charge_id=base.charge_id,
        tg_user_id=base.tg_user_id,
        amount=base.amount,
        currency=base.currency,
        kind=base.kind,
        invoice_payload=base.invoice_payload,
        is_recurring=base.is_recurring,
        is_first_recurring=base.is_first_recurring,
        period_end=base.period_end,
        raw=base.raw,
        source=FROM_RECONCILE,
        period_end_estimated=True,
    )

    await repo.insert_payment(user_id, record)
    await db_session.commit()

    row = await db_session.scalar(
        select(models.Payment).where(models.Payment.external_id == "found-later")
    )
    assert row is not None and row.source == "reconcile" and row.period_end_estimated is True
    plain = await repo.insert_payment(user_id, _record("from-update"))
    await db_session.commit()
    stored = await db_session.scalar(
        select(models.Payment).where(models.Payment.external_id == "from-update")
    )
    assert plain and stored is not None and stored.source == "update"


async def test_an_event_by_kind_and_charge_is_found(db_session: AsyncSession) -> None:
    repo = BillingRepository(db_session)
    await repo.record_event(
        BillingEvent(EventKind.RECONCILE_GAP, 42, {"stage": "gap"}, charge_id="gap-1")
    )
    await db_session.commit()

    assert await repo.has_event(EventKind.RECONCILE_GAP, "gap-1") is True
    assert await repo.has_event(EventKind.RECONCILE_GAP, "gap-2") is False
    assert await repo.has_event(EventKind.REFUND_STUCK, "gap-1") is False
