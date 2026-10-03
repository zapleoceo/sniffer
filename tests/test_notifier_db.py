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


async def _queued(session: AsyncSession) -> tuple[int, int]:
    """Одна карточка, поставленная в очередь настоящим `enqueue`: (outbox, notification)."""
    user = await UserRepository(session).get_or_create(555, username="подписчик")
    assert user.id is not None
    passport = Passport(intent=Intent.BUY, category=Category.MOTORBIKE, city="nha_trang")
    stored = await PassportRepository(session).save_new(user.id, passport)
    await session.flush()
    subscription = models.Subscription(user_id=user.id, passport_root=stored.id)
    session.add(subscription)
    await session.flush()
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
    assert card.id is not None and subscription.id is not None
    await DeliveryRepository(session).enqueue(
        subscription_id=subscription.id,
        user_id=user.id,
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
