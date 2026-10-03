"""Слоты мониторинга на живом Postgres: пересчёт по журналу, включение, перенос, продление.

Пропускается без `TEST_DATABASE_URL` (CI поднимает pgvector/pgvector:pg16). «Сейчас» —
настоящее: срок слота лежит в `subscriptions.expires_at`, а предикат права
(`delivery.entitled`) читает его в самом запросе монитора (`test_db_clock_rule.py`).
Мониторинг выбирается ровно так, как его выбирает воркер (`MonitorRepository.claim_due`).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db import models
from sniffer.db.repositories.billing import BillingRepository
from sniffer.db.repositories.monitors import MonitorRepository
from sniffer.db.repositories.passports import PassportRepository
from sniffer.db.repositories.slots import SlotRepository
from sniffer.db.repositories.users import UserRepository
from sniffer.domain.billing import PaymentKind, PaymentRecord
from sniffer.domain.passport import Category, Intent, Passport
from sniffer.domain.slots import Outcome
from tests.subscription_support import grant

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL не задан: живого Postgres нет",
)

NOW = datetime.now(UTC).replace(microsecond=0)
DAY = timedelta(days=1)


async def client(session: AsyncSession, tg_id: int = 4242) -> int:
    user = await UserRepository(session).get_or_create(tg_id, username="платящий")
    assert user.id is not None
    await session.commit()
    return user.id


async def branch(session: AsyncSession, user_id: int, number: int) -> int:
    passport = Passport(intent=Intent.BUY, category=Category.MOTORBIKE, city="nha_trang")
    stored = await PassportRepository(session).save_new(user_id, passport)
    await session.commit()
    del number
    return stored.root


async def pay(
    session: AsyncSession,
    user_id: int,
    charge: str,
    *,
    ends: datetime,
    kind: PaymentKind = PaymentKind.FIRST,
    payload: str | None = None,
) -> None:
    await BillingRepository(session).insert_payment(
        user_id,
        PaymentRecord(
            charge_id=charge,
            tg_user_id=4242,
            amount=10,
            currency="XTR",
            kind=kind,
            invoice_payload=payload or f"v2:s:4242:2026-10-03:{charge[:1] * 12}",
            is_recurring=True,
            is_first_recurring=kind is PaymentKind.FIRST,
            period_end=ends,
            raw={},
        ),
    )
    await session.commit()


async def working(session: AsyncSession, now: datetime = NOW) -> set[int]:
    """Корни, которые монитор взял бы в обход в момент `now`."""
    due = await MonitorRepository(session).claim_due(limit=200, now=now)
    return {state.passport_root for state in due.ready}


async def monitors(session: AsyncSession, user_id: int) -> list[models.Subscription]:
    rows = await session.scalars(
        select(models.Subscription)
        .where(models.Subscription.user_id == user_id)
        .order_by(models.Subscription.priority, models.Subscription.id)
        .execution_options(populate_existing=True)
    )
    return list(rows)


async def test_without_a_paid_subscription_tracking_is_not_enabled(
    db_session: AsyncSession,
) -> None:
    user_id = await client(db_session)
    root = await branch(db_session, user_id, 1)

    outcome, state = await SlotRepository(db_session).enable(user_id, root, NOW)
    await db_session.commit()

    assert outcome is Outcome.NEEDS_SUBSCRIPTION and state.slots == 0
    assert await monitors(db_session, user_id) == []


async def test_a_paid_subscription_gives_one_slot_and_the_monitor_picks_the_branch_up(
    db_session: AsyncSession,
) -> None:
    user_id = await client(db_session)
    root = await branch(db_session, user_id, 1)
    ends = NOW + 30 * DAY
    await pay(db_session, user_id, "alpha", ends=ends)

    outcome, state = await SlotRepository(db_session).enable(user_id, root, NOW)
    again, _ = await SlotRepository(db_session).enable(user_id, root, NOW)
    await db_session.commit()

    (row,) = await monitors(db_session, user_id)
    assert (outcome, again) == (Outcome.ENABLE, Outcome.ALREADY_ON)
    assert state.slots == 1 and state.holding == 1
    assert row.expires_at == ends, "срок слота — срок подписки от Telegram"
    assert await working(db_session) == {root}


async def test_new_tracking_starts_from_now_not_from_the_past(db_session: AsyncSession) -> None:
    user_id = await client(db_session)
    root = await branch(db_session, user_id, 1)
    await pay(db_session, user_id, "alpha", ends=NOW + 30 * DAY)
    newest = await db_session.scalar(select(func.coalesce(func.max(models.Listing.id), 0)))

    await SlotRepository(db_session).enable(user_id, root, NOW)

    (row,) = await monitors(db_session, user_id)
    assert row.since_listing_id == (newest or 0) and row.scan_listing_id == (newest or 0)


async def test_a_second_branch_without_a_free_slot_is_refused_and_nothing_is_created(
    db_session: AsyncSession,
) -> None:
    user_id = await client(db_session)
    first, second = await branch(db_session, user_id, 1), await branch(db_session, user_id, 2)
    await pay(db_session, user_id, "alpha", ends=NOW + 30 * DAY)
    await SlotRepository(db_session).enable(user_id, first, NOW)

    outcome, _ = await SlotRepository(db_session).enable(user_id, second, NOW)
    await db_session.commit()

    assert outcome is Outcome.NO_FREE_SLOT
    assert [m.passport_root for m in await monitors(db_session, user_id)] == [first]


async def test_a_second_subscription_adds_a_second_slot(db_session: AsyncSession) -> None:
    user_id = await client(db_session)
    first, second = await branch(db_session, user_id, 1), await branch(db_session, user_id, 2)
    await pay(db_session, user_id, "alpha", ends=NOW + 30 * DAY)
    await pay(db_session, user_id, "bravo", ends=NOW + 10 * DAY)
    repo = SlotRepository(db_session)

    await repo.enable(user_id, first, NOW)
    outcome, state = await repo.enable(user_id, second, NOW)
    await db_session.commit()

    assert outcome is Outcome.ENABLE and state.slots == 2 and state.holding == 2
    ends = {m.passport_root: m.expires_at for m in await monitors(db_session, user_id)}
    assert ends == {first: NOW + 30 * DAY, second: NOW + 10 * DAY}, "старшему — долгий срок"
    assert await working(db_session) == {first, second}


async def test_a_move_gives_the_slot_to_the_target_and_deletes_nothing(
    db_session: AsyncSession,
) -> None:
    user_id = await client(db_session)
    source, target = await branch(db_session, user_id, 1), await branch(db_session, user_id, 2)
    await pay(db_session, user_id, "alpha", ends=NOW + 30 * DAY)
    repo = SlotRepository(db_session)
    await repo.enable(user_id, source, NOW)

    moved = await repo.move(user_id, to_root=target, from_root=source, now=NOW)
    await db_session.commit()

    assert moved is True
    assert await working(db_session) == {target}
    rows = await monitors(db_session, user_id)
    assert {m.passport_root for m in rows} == {source, target}, "прежний мониторинг цел"
    again = await repo.move(user_id, to_root=source, from_root=target, now=NOW)
    await db_session.commit()
    assert again is True and await working(db_session) == {source}, "слот возвращается так же"


async def test_a_move_from_a_branch_without_a_slot_is_refused(db_session: AsyncSession) -> None:
    user_id = await client(db_session)
    first, second = await branch(db_session, user_id, 1), await branch(db_session, user_id, 2)
    await pay(db_session, user_id, "alpha", ends=NOW + 30 * DAY)
    await SlotRepository(db_session).enable(user_id, first, NOW)

    moved = await SlotRepository(db_session).move(user_id, to_root=first, from_root=second, now=NOW)

    assert moved is False


async def test_a_lapsed_subscription_pauses_the_monitor_and_a_renewal_resumes_it(
    db_session: AsyncSession,
) -> None:
    """Просрочка → пауза без удаления; продление → мониторинг сам возвращается в обход."""
    user_id = await client(db_session)
    root = await branch(db_session, user_id, 1)
    ends = NOW + 30 * DAY
    await pay(db_session, user_id, "alpha", ends=ends)
    await SlotRepository(db_session).enable(user_id, root, NOW)
    after = ends + DAY

    assert await working(db_session, now=after) == set(), "срок вышел: слота нет, по самой дате"
    assert len(await monitors(db_session, user_id)) == 1, "ничего не удалено"

    await pay(
        db_session,
        user_id,
        "alpha-2",
        ends=after + 29 * DAY,
        kind=PaymentKind.RENEWAL,
        payload="v2:s:4242:2026-10-03:" + "a" * 12,
    )
    state = await SlotRepository(db_session).sync(user_id, after)
    await db_session.commit()

    assert state.resumed == 1 and state.holding == 1
    assert await working(db_session, now=after) == {root}


async def test_a_refund_takes_the_slot_away_at_once(db_session: AsyncSession) -> None:
    user_id = await client(db_session)
    root = await branch(db_session, user_id, 1)
    await pay(db_session, user_id, "alpha", ends=NOW + 30 * DAY)
    await SlotRepository(db_session).enable(user_id, root, NOW)
    assert await working(db_session) == {root}

    await BillingRepository(db_session).mark_refunding("alpha")
    state = await SlotRepository(db_session).sync(user_id, NOW)
    await db_session.commit()

    assert state.slots == 0 and state.holding == 0
    assert await working(db_session) == set()
    assert len(await monitors(db_session, user_id)) == 1


async def test_a_manual_grant_without_a_term_is_not_touched_by_the_recount(
    db_session: AsyncSession,
) -> None:
    user_id = await client(db_session)
    root = await branch(db_session, user_id, 1)
    await grant(db_session, user_id, root, until=None)
    await db_session.commit()

    await SlotRepository(db_session).sync(user_id, NOW)
    await db_session.commit()

    (row,) = await monitors(db_session, user_id)
    assert row.expires_at is None
    assert await working(db_session) == {root}


async def test_a_recount_changes_only_the_clients_own_monitors(db_session: AsyncSession) -> None:
    mine, other = await client(db_session, 4242), await client(db_session, 5151)
    their_root = await branch(db_session, other, 1)
    await grant(db_session, other, their_root, until=NOW + 5 * DAY)
    await db_session.commit()

    await SlotRepository(db_session).sync(mine, NOW)
    await db_session.commit()

    (row,) = await monitors(db_session, other)
    assert row.expires_at == NOW + 5 * DAY
