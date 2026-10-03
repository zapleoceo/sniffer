"""Очередь доставки на живом Postgres: запирание строк, коммит на сообщение, метки.

Пропускается, пока не задан `TEST_DATABASE_URL` (см. `conftest.py`): проверять
`FOR UPDATE SKIP LOCKED` и транзакции не на Postgres бессмысленно — на подделке
они зелёные всегда. Время ставится явно, а не колонкой `DEFAULT now()`: тест,
который на неё полагается, проверяет часы машины, а не запрос.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sniffer.db import models
from sniffer.db.repositories import (
    ListingRepository,
    PassportRepository,
    RawMessageRepository,
    UserRepository,
)
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.domain.passport import Category, Intent, Passport
from sniffer.domain.records import Listing, RawMessage
from sniffer.notifier import ports
from sniffer.notifier.delivery import Delivery
from tests.bot_api_support import refusal, telegram_error
from tests.notifier_support import PAYLOAD, Boom, Clock

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL не задан: живого Postgres нет",
)

# Настоящее «сейчас», а не зашитая дата: см. tests/test_db_clock_rule.py.
NOW = datetime.now(UTC).replace(microsecond=0)


class Recorder:
    """Подставной Bot API: отвечает по сценарию, секунду «занимает» и ничего не знает о базе."""

    def __init__(self, clock: Clock, *outcomes: BaseException | None) -> None:
        self._clock = clock
        self._outcomes = list(outcomes)
        self.recipients: list[int] = []

    async def __call__(self, user_id: int, text: str) -> None:
        self.recipients.append(user_id)
        outcome = self._outcomes.pop(0) if self._outcomes else None
        if outcome is not None:
            raise outcome
        self._clock.advance(seconds=1)


async def _clients(session: AsyncSession, count: int) -> list[int]:
    """Клиенты с разными Telegram id: у каждого своя строка очереди, дайджестов нет."""
    ids = []
    for number in range(count):
        user = await UserRepository(session).get_or_create(900 + number)
        assert user.id is not None
        ids.append(user.id)
    return ids


async def _rows(session: AsyncSession, user_ids: list[int], **overrides: object) -> list[int]:
    """По строке очереди на клиента, все давно созрели. Возврат — их `id`."""
    rows = [
        models.Outbox(
            user_id=user_id,
            payload=dict(PAYLOAD),
            scheduled_at=NOW - timedelta(minutes=5),
            **overrides,
        )
        for user_id in user_ids
    ]
    session.add_all(rows)
    await session.commit()
    return [row.id for row in rows]


async def _subscriber(session: AsyncSession, tg_id: int = 555, **fields: Any) -> tuple[int, int]:
    """Клиент с подпиской на свой паспорт: (users.id, subscriptions.id)."""
    user = await UserRepository(session).get_or_create(tg_id, username="подписчик")
    assert user.id is not None
    passport = Passport(intent=Intent.BUY, category=Category.MOTORBIKE, city="nha_trang")
    stored = await PassportRepository(session).save_new(user.id, passport)
    await session.flush()
    subscription = models.Subscription(user_id=user.id, passport_root=stored.id, **fields)
    session.add(subscription)
    await session.flush()
    assert subscription.id is not None
    return user.id, subscription.id


async def _queued(session: AsyncSession) -> tuple[int, int]:
    """Одна карточка, поставленная в очередь настоящим `enqueue`: (outbox, notification)."""
    user_id, subscription_id = await _subscriber(session)
    raw = RawMessage(
        chat_tg_id=-100123, msg_id=1, text="Продам Honda Vision", text_hash="hash-1", posted_at=NOW
    )
    (raw_id,) = await RawMessageRepository(session).add_many([raw])
    card = await ListingRepository(session).add(
        Listing(
            raw_message_id=raw_id,
            deal_type="sell",
            category="motorbike",
            city="nha_trang",
            title="Honda Vision",
            summary="Автомат",
            tg_link="https://t.me/c/1/1",
            posted_at=NOW,
        )
    )
    assert card.id is not None
    await DeliveryRepository(session).enqueue(
        subscription_id=subscription_id,
        user_id=user_id,
        listing_id=card.id,
        score=0.9,
        payload=dict(PAYLOAD),
        scheduled_at=NOW - timedelta(minutes=1),
    )
    await session.commit()
    outbox = await session.scalar(select(models.Outbox.id))
    notification = await session.scalar(select(models.Notification.id))
    assert outbox is not None and notification is not None
    return outbox, notification


# ── запирание строк ─────────────────────────────────────────────────────────


async def test_lock_pending_returns_only_pending_rows_that_are_due(
    db_session: AsyncSession,
) -> None:
    (user_id,) = await _clients(db_session, 1)
    (due,) = await _rows(db_session, [user_id])
    (sent,) = await _rows(db_session, [user_id], status="sent")
    (later,) = await _rows(db_session, [user_id])
    await db_session.execute(
        update(models.Outbox)
        .where(models.Outbox.id == later)
        .values(scheduled_at=NOW + timedelta(hours=1))
    )
    await db_session.commit()

    held = await DeliveryRepository(db_session).lock_pending([due, sent, later], now=NOW)

    assert [message.id for message in held] == [due]


async def test_lock_pending_skips_a_row_another_transaction_holds(db_engine: AsyncEngine) -> None:
    """Ради этого очередь и живёт в Postgres: вторая копия не ждёт, а проходит мимо."""
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as setup:
        ids = await _rows(setup, await _clients(setup, 2))

    # Транзакция первой копии намеренно не закрыта, пока вторая берёт своё.
    async with sessions() as first, sessions() as second:
        mine = await DeliveryRepository(first).lock_pending(ids, now=NOW)
        theirs = await DeliveryRepository(second).lock_pending(ids, now=NOW)

    assert len(mine) == 2 and theirs == [], "одну строку нельзя отправить двумя копиями"


async def test_take_pending_does_not_hold_locks_for_the_whole_pass(db_engine: AsyncEngine) -> None:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as setup:
        ids = await _rows(setup, await _clients(setup, 2))

    async with sessions() as planner, sessions() as sender:
        planned = await DeliveryRepository(planner).take_pending(limit=20, now=NOW)
        held = await DeliveryRepository(sender).lock_pending(ids, now=NOW)

    assert len(planned) == 2 and len(held) == 2, "чтение запрещало отправку: проход держит замки"


async def test_mark_sent_stamps_the_given_moment_on_the_row_and_its_notification(
    db_session: AsyncSession,
) -> None:
    outbox_id, notification_id = await _queued(db_session)
    moment = NOW + timedelta(seconds=7)

    await DeliveryRepository(db_session).mark_sent(outbox_id, now=moment)
    await db_session.commit()

    row = await db_session.get(models.Outbox, outbox_id)
    notification = await db_session.get(models.Notification, notification_id)
    assert row is not None and notification is not None
    assert (row.status, row.sent_at) == ("sent", moment)
    assert notification.sent_at == moment


# ── коммит на сообщение, на настоящей базе ──────────────────────────────────


@asynccontextmanager
async def _own_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session


async def test_delivery_commits_each_message_on_a_real_database(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой второй отправки не откатывает первую, а `sent_at` — момент подтверждения."""
    monkeypatch.setattr(ports, "session_scope", lambda: _own_session(db_engine))
    async with _own_session(db_engine) as setup:
        ids = await _rows(setup, await _clients(setup, 3))
    clock = Clock(NOW)
    telegram = Recorder(clock, None, Boom("сбой на втором"), None)

    sent = await Delivery(telegram, pause_s=0.0, clock=clock).tick(now=NOW)

    assert sent == 2 and len(telegram.recipients) == 3
    async with _own_session(db_engine) as check:
        rows = {row.id: row for row in await check.scalars(select(models.Outbox))}
    first, second, third = (rows[i] for i in ids)
    assert (first.status, second.status, third.status) == ("sent", "pending", "sent")
    assert second.attempts == 1
    assert first.sent_at == NOW + timedelta(seconds=1)
    assert third.sent_at == NOW + timedelta(seconds=2)


# ── отказ Telegram, причина и блокировка ────────────────────────────────────


async def test_give_up_and_mark_failed_keep_the_reason_and_count_the_attempt(
    db_session: AsyncSession,
) -> None:
    first, second = await _rows(db_session, await _clients(db_session, 2))
    repo = DeliveryRepository(db_session)

    await repo.mark_failed(first, retry_at=NOW + timedelta(minutes=1), error="transient: сеть")
    await repo.give_up(second, error="too_long: ...")
    await db_session.commit()

    retried = await db_session.get(models.Outbox, first)
    refused = await db_session.get(models.Outbox, second)
    assert retried is not None and refused is not None
    assert (retried.status, retried.attempts, retried.last_error) == (
        "pending",
        1,
        "transient: сеть",
    )
    assert (refused.status, refused.attempts, refused.last_error) == ("failed", 1, "too_long: ...")


async def test_mark_sent_clears_the_reason_of_an_earlier_failure(db_session: AsyncSession) -> None:
    (row_id,) = await _rows(db_session, await _clients(db_session, 1), last_error="transient: сеть")

    await DeliveryRepository(db_session).mark_sent(row_id, now=NOW)
    await db_session.commit()

    row = await db_session.get(models.Outbox, row_id)
    assert row is not None and (row.status, row.last_error) == ("sent", None)


async def test_cancelling_a_clients_queue_leaves_everyone_elses_and_sent_rows_alone(
    db_session: AsyncSession,
) -> None:
    mine, other = await _clients(db_session, 2)
    (waiting,) = await _rows(db_session, [mine])
    (delivered,) = await _rows(db_session, [mine], status="sent")
    (strangers,) = await _rows(db_session, [other])

    cancelled = await DeliveryRepository(db_session).cancel_pending_of(mine, reason="forbidden")
    await db_session.commit()

    assert cancelled == 1
    rows = {row.id: row for row in await db_session.scalars(select(models.Outbox))}
    assert (rows[waiting].status, rows[waiting].last_error) == ("cancelled", "forbidden")
    assert rows[delivered].status == "sent" and rows[strangers].status == "pending"


async def test_blocking_keeps_the_first_moment_and_unblocking_clears_it(
    db_session: AsyncSession,
) -> None:
    users = UserRepository(db_session)
    (user_id,) = await _clients(db_session, 1)
    first, later = NOW, NOW + timedelta(hours=1)

    assert await users.set_bot_blocked(900, blocked=True, at=first) == user_id
    await users.set_bot_blocked(900, blocked=True, at=later)
    await db_session.commit()
    blocked = await db_session.get(models.User, user_id)
    assert blocked is not None and blocked.bot_blocked_at == first, "повтор отказа сдвинул момент"

    await users.set_bot_blocked(900, blocked=False, at=later)
    await db_session.commit()
    await db_session.refresh(blocked)
    assert blocked.bot_blocked_at is None
    assert await users.set_bot_blocked(123456, blocked=True, at=first) is None, "чужого не заводим"


async def test_rows_queued_after_the_block_are_cancelled_by_the_sweep(
    db_session: AsyncSession,
) -> None:
    blocked_id, fine_id = await _clients(db_session, 2)
    await UserRepository(db_session).set_bot_blocked(900, blocked=True, at=NOW)
    (stray,) = await _rows(db_session, [blocked_id])
    (healthy,) = await _rows(db_session, [fine_id])

    cancelled = await DeliveryRepository(db_session).cancel_for_blocked_users(reason="bot_blocked")
    await db_session.commit()

    assert cancelled == 1
    rows = {row.id: row for row in await db_session.scalars(select(models.Outbox))}
    assert (rows[stray].status, rows[stray].last_error) == ("cancelled", "bot_blocked")
    assert rows[healthy].status == "pending"


async def test_the_matcher_selection_skips_a_blocked_client_and_resumes_after_unblocking(
    db_session: AsyncSession,
) -> None:
    """Пауза слежения выведена запросом: разблокировал — возобновилось само, подписка цела."""
    _, subscription_id = await _subscriber(db_session)
    await db_session.commit()
    repo, users = DeliveryRepository(db_session), UserRepository(db_session)
    assert [item.id for item in await repo.active_subscriptions()] == [subscription_id]

    await users.set_bot_blocked(555, blocked=True, at=NOW)
    await db_session.commit()
    assert await repo.active_subscriptions() == []

    await users.set_bot_blocked(555, blocked=False, at=NOW)
    await db_session.commit()
    assert [item.id for item in await repo.active_subscriptions()] == [subscription_id]


async def test_a_403_on_a_real_database_blocks_the_client_and_cancels_their_queue(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ports, "session_scope", lambda: _own_session(db_engine))
    async with _own_session(db_engine) as setup:
        mine, other = await _clients(setup, 2)
        ids = await _rows(setup, [mine, mine, other])
    clock = Clock(NOW)
    forbidden = telegram_error(refusal(403, "Forbidden: bot was blocked by the user"))
    telegram = Recorder(clock, forbidden)

    assert await Delivery(telegram, pause_s=0.0, clock=clock).tick(now=NOW) == 1

    async with _own_session(db_engine) as check:
        rows = {row.id: row for row in await check.scalars(select(models.Outbox))}
        user = await check.get(models.User, mine)
    assert [rows[i].status for i in ids] == ["cancelled", "cancelled", "sent"]
    assert (rows[ids[0]].last_error or "").startswith("forbidden")
    assert user is not None and user.bot_blocked_at == NOW
    assert telegram.recipients == [900, 901], "второе сообщение заблокировавшего не отправлялось"


# ── срок годности ───────────────────────────────────────────────────────────


async def test_expired_rows_are_cancelled_with_the_reason_each_term_gives(
    db_session: AsyncSession,
) -> None:
    """Сутки для всех, шесть часов — когда у подписки нет права: срок вышел или пауза."""
    hour = timedelta(hours=1)
    entitled_user, entitled = await _subscriber(db_session, 601, expires_at=NOW + 5 * hour)
    lapsed_user, lapsed = await _subscriber(db_session, 602, expires_at=NOW - hour)
    paused_user, paused = await _subscriber(db_session, 603, is_active=False)
    plain_user = (await _clients(db_session, 1))[0]
    plan = {
        "active_7h": (entitled_user, entitled, 7 * hour, "pending", None),
        "active_25h": (entitled_user, entitled, 25 * hour, "cancelled", "expired"),
        "lapsed_7h": (lapsed_user, lapsed, 7 * hour, "cancelled", "right_lost"),
        "lapsed_5h": (lapsed_user, lapsed, 5 * hour, "pending", None),
        "paused_7h": (paused_user, paused, 7 * hour, "cancelled", "right_lost"),
        "plain_7h": (plain_user, None, 7 * hour, "pending", None),
        "plain_25h": (plain_user, None, 25 * hour, "cancelled", "expired"),
    }
    rows = {
        name: models.Outbox(
            user_id=user_id,
            subscription_id=subscription_id,
            payload=dict(PAYLOAD),
            scheduled_at=NOW - age,
        )
        for name, (user_id, subscription_id, age, _, _) in plan.items()
    }
    delivered = models.Outbox(
        user_id=plain_user, payload=dict(PAYLOAD), scheduled_at=NOW - 30 * hour, status="sent"
    )
    db_session.add_all([*rows.values(), delivered])
    await db_session.commit()

    cancelled = await DeliveryRepository(db_session).cancel_expired(
        now=NOW, ttl=24 * hour, lost_right_ttl=6 * hour
    )
    await db_session.commit()

    assert cancelled == 4
    for name, row in rows.items():
        await db_session.refresh(row)
        _, _, _, status, reason = plan[name]
        assert (row.status, row.last_error) == (status, reason), name
    await db_session.refresh(delivered)
    assert delivered.status == "sent", "ушедшее срок не трогает"


async def test_a_subscription_without_an_end_date_keeps_its_right(db_session: AsyncSession) -> None:
    """`expires_at IS NULL` — бессрочная (так сегодня читает и матчер), а не просроченная."""
    user_id, subscription_id = await _subscriber(db_session, 604)
    row = models.Outbox(
        user_id=user_id,
        subscription_id=subscription_id,
        payload=dict(PAYLOAD),
        scheduled_at=NOW - timedelta(hours=7),
    )
    db_session.add(row)
    await db_session.commit()

    cancelled = await DeliveryRepository(db_session).cancel_expired(
        now=NOW, ttl=timedelta(hours=24), lost_right_ttl=timedelta(hours=6)
    )

    assert cancelled == 0
