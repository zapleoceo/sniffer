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

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sniffer.db import models
from sniffer.db.repositories import (
    ListingRepository,
    PassportRepository,
    RawMessageRepository,
    UserRepository,
)
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.db.repositories.monitors import MonitorRepository
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.domain.records import Listing, RawMessage
from sniffer.worker.quarantine import quarantine_delay

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


# ── обход по кругу (D4) ─────────────────────────────────────────────────────


async def _scanned(session: AsyncSession) -> set[int]:
    rows = await session.scalars(
        select(models.Subscription.id).where(models.Subscription.last_scanned_at.is_not(None))
    )
    return set(rows)


async def test_every_subscription_gets_a_turn_when_there_are_more_than_one_batch(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Порция в две подписки на пять: за три прохода каждая побывала в обходе.

    Прежний `ORDER BY id LIMIT 50` отдавал каждый проход одни и те же первые строки.
    """
    from sniffer.worker import matcher as module

    now = _now()
    ids = {(await _slot(db_session, 9100 + number, expires_at=None))[1] for number in range(5)}
    await db_session.commit()
    monkeypatch.setattr(module, "session_scope", lambda: _borrowed(db_session))
    matcher = module.Matcher(batch=2)

    seen = []
    for step in range(3):
        await matcher.tick(now=now + timedelta(seconds=step))
        seen.append(await _scanned(db_session))

    assert [len(turn) for turn in seen] == [2, 4, 5], "каждый проход берёт ещё не смотренных"
    assert seen[-1] == ids


async def test_more_than_fifty_subscriptions_are_all_served(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """51-я и дальше подписки не прятались бы вечно: порция по умолчанию — 50 (D4)."""
    from sniffer.worker import matcher as module

    now = _now()
    ids = {(await _slot(db_session, 9400 + number, expires_at=None))[1] for number in range(60)}
    await db_session.commit()
    monkeypatch.setattr(module, "session_scope", lambda: _borrowed(db_session))
    matcher = module.Matcher()

    await matcher.tick(now=now)
    assert len(await _scanned(db_session)) == 50
    await matcher.tick(now=now + timedelta(seconds=5))

    assert await _scanned(db_session) == ids


async def test_the_claim_serves_the_least_recently_scanned_first(db_session: AsyncSession) -> None:
    now = _now()
    _a, long_ago = await _slot(db_session, 9500, expires_at=None)
    _b, never = await _slot(db_session, 9501, expires_at=None)
    _c, recently = await _slot(db_session, 9502, expires_at=None)
    scans = {long_ago: now - timedelta(hours=2), recently: now - timedelta(hours=1)}
    for sub_id, scanned in scans.items():
        await db_session.execute(
            update(models.Subscription)
            .where(models.Subscription.id == sub_id)
            .values(last_scanned_at=scanned)
        )
    await db_session.commit()

    due = await MonitorRepository(db_session).claim_due(limit=10, now=now)

    assert [state.id for state in due.ready] == [never, long_ago, recently]


async def test_a_slot_held_by_another_pass_is_skipped_not_waited_for(
    db_engine: AsyncEngine,
) -> None:
    """Две копии воркера не берут одну подписку и не ждут друг друга (`SKIP LOCKED`)."""
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    now = _now()
    async with sessions() as setup:
        _u1, first = await _slot(setup, 9301, expires_at=None)
        _u2, second = await _slot(setup, 9302, expires_at=None)
        await setup.commit()

    async with sessions() as holder, sessions() as other:
        held = await MonitorRepository(holder).claim_due(limit=1, now=now)
        assert [state.id for state in held.ready] == [first]

        free = await asyncio.wait_for(
            MonitorRepository(other).claim_due(limit=10, now=now), timeout=10
        )

        assert [state.id for state in free.ready] == [second], "занятая строка пропущена"
        await holder.rollback()
        await other.rollback()


# ── больная подписка и карантин (D5) ────────────────────────────────────────


async def _poison(session: AsyncSession, sub_id: int, category: str = "spaceship") -> None:
    """Незнакомое значение в паспорте — то, что остаётся в базе после отката кода."""
    root = await session.scalar(
        select(models.Subscription.passport_root).where(models.Subscription.id == sub_id)
    )
    await session.execute(
        update(models.Passport).where(models.Passport.id == root).values(category=category)
    )


async def _failures(session: AsyncSession, sub_id: int) -> tuple[int, str | None, datetime | None]:
    row = (
        await session.execute(
            select(
                models.Subscription.failed_streak,
                models.Subscription.last_error,
                models.Subscription.quarantined_until,
            ).where(models.Subscription.id == sub_id)
        )
    ).one()
    return row.failed_streak, row.last_error, row.quarantined_until


async def test_a_poisoned_passport_quarantines_only_its_own_slot(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Прежде такая строка валила разбор всей пачки и воркер вместе с ней (D5)."""
    from sniffer.worker import matcher as module

    now = _now()
    _h, healthy = await _slot(db_session, 9201, expires_at=None)
    _s, sick = await _slot(db_session, 9202, expires_at=None)
    await _poison(db_session, sick)
    await _card(db_session, 20, posted_at=now - timedelta(hours=1))
    await db_session.commit()
    monkeypatch.setattr(module, "session_scope", lambda: _borrowed(db_session))

    assert await module.Matcher().tick(now=now) == 1, "здоровая получила карточку, проход жив"

    streak, error, until = await _failures(db_session, sick)
    assert streak == 1 and until == now + quarantine_delay(1)
    assert error is not None and "spaceship" in error
    assert await _failures(db_session, healthy) == (0, None, None)


async def test_a_quarantined_slot_comes_back_by_itself_when_its_time_has_come(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sniffer.worker import matcher as module

    start = _now()
    _s, sick = await _slot(db_session, 9210, expires_at=None)
    await _poison(db_session, sick)
    await db_session.commit()
    monkeypatch.setattr(module, "session_scope", lambda: _borrowed(db_session))

    await module.Matcher().tick(now=start)
    assert (await _failures(db_session, sick))[0] == 1

    await module.Matcher().tick(now=start + timedelta(minutes=4))
    assert (await _failures(db_session, sick))[0] == 1, "пауза не вышла: подписку не трогали"

    second = start + quarantine_delay(1)
    await module.Matcher().tick(now=second)
    streak, _error, until = await _failures(db_session, sick)
    assert streak == 2, "ровно в срок подписка снова в обходе, и снова падает"
    assert until == second + quarantine_delay(2)

    await _poison(db_session, sick, category="motorbike")
    await db_session.commit()
    await module.Matcher().tick(now=until)
    assert await _failures(db_session, sick) == (0, None, None), "починили — карантин снят"


async def test_a_failure_after_the_enqueue_rolls_back_only_that_slots_rows(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SAVEPOINT откатывает уже поставленную карточку больной подписки, но не соседней."""
    from sniffer.worker import matcher as module

    now = _now()
    _h, healthy = await _slot(db_session, 9220, expires_at=None)
    _s, sick = await _slot(db_session, 9221, expires_at=None)
    await _card(db_session, 21, posted_at=now - timedelta(hours=1))
    await db_session.commit()
    monkeypatch.setattr(module, "session_scope", lambda: _borrowed(db_session))
    real = DeliveryRepository.advance_scan

    async def flaky(self: DeliveryRepository, subscription_id: int, listing_id: int) -> None:
        if subscription_id == sick:
            raise RuntimeError("упали уже после постановки в очередь")
        await real(self, subscription_id, listing_id)

    monkeypatch.setattr(DeliveryRepository, "advance_scan", flaky)

    assert await module.Matcher().tick(now=now) == 1

    async def queued_for(sub_id: int) -> tuple[int, int]:
        notes = await db_session.scalar(
            select(func.count()).where(models.Notification.subscription_id == sub_id)
        )
        mail = await db_session.scalar(
            select(func.count()).where(models.Outbox.subscription_id == sub_id)
        )
        return int(notes or 0), int(mail or 0)

    assert await queued_for(healthy) == (1, 1)
    assert await queued_for(sick) == (0, 0), "записи больной подписки откатились целиком"
    streak, error, _until = await _failures(db_session, sick)
    assert streak == 1 and error is not None and "RuntimeError" in error
