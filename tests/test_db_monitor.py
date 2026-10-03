"""Монитор подписок на живом Postgres: часы, обход, изоляция, право на доставку.

Пропускаются без `TEST_DATABASE_URL` (см. `conftest.py`): держится всё на свойствах самого
Postgres — `FOR UPDATE SKIP LOCKED`, SAVEPOINT, `ON CONFLICT`, `RETURNING`, порядок
`NULLS FIRST`, — и подделка проверяла бы подделку. Тот же проход без базы, на подменённых
репозиториях, — `test_monitor_pass.py`; форму запросов без базы держит `test_monitor_sql.py`.

Время в этих тестах — настоящее «сейчас» плюс смещения, а не зашитая дата (правило
`test_db_clock_rule.py`): срок подписки и окна карточек сверяются с тем, что проход получил
аргументом, и зашитое число протухло бы через месяц само.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.domain.records import Listing, RawMessage

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL не задан: живого Postgres нет",
)


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _passport(**overrides: object) -> Passport:
    fields: dict[str, object] = {
        "intent": Intent.BUY,
        "category": Category.MOTORBIKE,
        "city": "nha_trang",
        "raw_query": "ищу скутер",
    }
    fields.update(overrides)
    return Passport(**fields)  # type: ignore[arg-type]


async def _slot(
    session: AsyncSession,
    tg_id: int,
    *,
    expires_at: datetime | None,
    passport: Passport | None = None,
) -> tuple[int, int]:
    """Клиент с подпиской на свой запрос. Возврат: (user_id, subscription_id)."""
    user = await UserRepository(session).get_or_create(tg_id)
    assert user.id is not None
    stored = await PassportRepository(session).save_new(user.id, passport or _passport())
    row = models.Subscription(user_id=user.id, passport_root=stored.id, expires_at=expires_at)
    session.add(row)
    await session.flush()
    return user.id, row.id


async def _card(
    session: AsyncSession, number: int, *, posted_at: datetime, price: Decimal | None = None
) -> Listing:
    (raw_id,) = await RawMessageRepository(session).add_many(
        [
            RawMessage(
                chat_tg_id=-100123,
                msg_id=number,
                text=f"Продам Honda Vision, сообщение {number}",
                text_hash=f"monitor-{number}",
                posted_at=posted_at,
            )
        ]
    )
    return await ListingRepository(session).add(
        Listing(
            raw_message_id=raw_id,
            deal_type="sell",
            category="motorbike",
            city="nha_trang",
            title=f"Honda Vision {number}",
            summary="Автомат",
            tg_link=f"https://t.me/c/1/{number}",
            posted_at=posted_at,
            price_amount=price,
            price_currency="VND" if price is not None else None,
        )
    )


@asynccontextmanager
async def _borrowed(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Сессия теста вместо своей: проверяем запросы, а не сборку соединения."""
    yield session


# ── часы (D6) ───────────────────────────────────────────────────────────────


async def test_enqueue_stamps_the_slot_with_the_given_moment(db_session: AsyncSession) -> None:
    """Суточный слот занимает `created_at`, и он обязан быть моментом прохода.

    С `DEFAULT now()` время постановки брала база: проход «из другого времени» (тест, догон
    после простоя) считал бы суточный лимит по чужим часам.
    """
    moment = _now() - timedelta(days=3)
    user_id, sub_id = await _slot(db_session, 9001, expires_at=None)
    card = await _card(db_session, 1, posted_at=moment)
    await db_session.commit()
    assert card.id is not None

    repo = DeliveryRepository(db_session)
    queued = await repo.enqueue(
        subscription_id=sub_id,
        user_id=user_id,
        listing_id=card.id,
        score=0.9,
        payload={"a": 1},
        now=moment,
    )
    await db_session.commit()

    assert queued is True
    stamped = await db_session.scalar(
        select(models.Notification.created_at).where(models.Notification.subscription_id == sub_id)
    )
    assert stamped == moment
    assert await repo.used_since(sub_id, since=moment - timedelta(seconds=1)) == 1
    assert await repo.used_since(sub_id, since=moment + timedelta(seconds=1)) == 0
    (message,) = await repo.take_pending(now=moment)
    assert message.scheduled_at == moment, "время доставки по умолчанию — момент прохода"


async def test_the_matcher_judges_the_term_by_the_moment_it_was_given(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Срок подписки сверяется с `now` прохода, а не с часами машины.

    Подписка оплачена на сутки вперёд. В «+3 суток» она закончилась, и проход обязан это
    видеть, даже если настоящее время ещё не дошло. Прежний выбор подписок брал часы
    процесса и обслуживал её.
    """
    from sniffer.worker import matcher as module

    now = _now()
    moment = now + timedelta(days=3)
    await _slot(db_session, 9002, expires_at=now + timedelta(days=1))
    await _card(db_session, 2, posted_at=moment - timedelta(hours=1))
    await db_session.commit()
    monkeypatch.setattr(module, "session_scope", lambda: _borrowed(db_session))

    assert await module.Matcher().tick(now=moment) == 0, "срок вышел, карточка не уходит"

    # Контроль: данные не виноваты. В момент, когда подписка жива, та же карточка уходит,
    # и время постановки — ровно момент прохода.
    assert await module.Matcher().tick(now=now) == 1
    stamped = await db_session.scalar(select(models.Notification.created_at))
    assert stamped == now


async def test_set_active_judges_the_term_by_the_moment_it_was_given(
    db_session: AsyncSession,
) -> None:
    """Возобновить можно только оплаченный мониторинг — оплаченный на тот момент, что дали."""
    now = _now()
    user_id, sub_id = await _slot(db_session, 9003, expires_at=now + timedelta(days=1))
    root = await db_session.scalar(
        select(models.Subscription.passport_root).where(models.Subscription.id == sub_id)
    )
    assert root is not None
    await db_session.commit()
    repo = DeliveryRepository(db_session)

    after_term = now + timedelta(days=2)
    assert not await repo.set_active(
        user_id=user_id, passport_root=root, active=False, now=after_term
    )
    assert await repo.set_active(user_id=user_id, passport_root=root, active=False, now=now)


# ── курс доллара (D2) ───────────────────────────────────────────────────────


def _dollars(amount: float) -> Passport:
    return _passport(budget=Budget(max=amount, currency=Currency.USD))


async def test_a_dollar_budget_narrows_the_real_query(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """300 $ при 26 000 ₫/$ — потолок 7,8 млн: дорогая карточка не уходит, дешёвая уходит."""
    from sniffer.worker import matcher as module

    now = _now()
    await _slot(db_session, 9010, expires_at=None, passport=_dollars(300))
    hour_ago = now - timedelta(hours=1)
    cheap = await _card(db_session, 10, posted_at=hour_ago, price=Decimal("5000000"))
    await _card(db_session, 11, posted_at=hour_ago, price=Decimal("20000000"))
    await db_session.commit()
    monkeypatch.setattr(module, "session_scope", lambda: _borrowed(db_session))

    async def rate() -> float | None:
        return 26_000.0

    assert await module.Matcher(rate=rate).tick(now=now) == 1

    (message,) = await DeliveryRepository(db_session).take_pending(now=now)
    assert message.payload["listing_id"] == cheap.id


async def test_a_dollar_slot_without_a_rate_keeps_its_cursor_and_catches_up_later(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без курса подписка ждёт и ничего не теряет: курсор стоит, потом карточка уходит."""
    from sniffer.worker import matcher as module

    now = _now()
    _user_id, sub_id = await _slot(db_session, 9011, expires_at=None, passport=_dollars(300))
    await _card(db_session, 12, posted_at=now - timedelta(hours=1), price=Decimal("5000000"))
    await db_session.commit()
    monkeypatch.setattr(module, "session_scope", lambda: _borrowed(db_session))

    async def down() -> float | None:
        return None

    async def up() -> float | None:
        return 26_000.0

    assert await module.Matcher(rate=down).tick(now=now) == 0
    cursor = await db_session.scalar(
        select(models.Subscription.scan_listing_id).where(models.Subscription.id == sub_id)
    )
    assert cursor == 0, "курсор не двигался: карточка не просмотрена"

    assert await module.Matcher(rate=up).tick(now=now + timedelta(minutes=2)) == 1
