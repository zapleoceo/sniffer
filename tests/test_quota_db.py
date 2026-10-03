"""Квота на живом Postgres: гонки, барьеры самой базы, формула границ.

То, чего подделка в памяти не воспроизводит принципиально: блокировка строки
периода под параллельными ответами, CHECK границ, составные внешние ключи,
уникальность. Сценарии контракта на тех же таблицах — в `test_quota_ledger.py`.
Пропускается без `TEST_DATABASE_URL` (см. `conftest.py`): локально без Docker
здесь проверять нечего, а в CI поднимается `pgvector/pgvector:pg16`.

Фиксированные моменты названы `T0`, а не `NOW`: квотный SQL не сверяет ничего с
часами базы, «сейчас» ему передают параметром (см. `test_db_clock_rule.py`).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from sniffer.bot.quota import QuotaService
from sniffer.db import collection_models, models
from sniffer.domain.quota_period import add_months
from tests.quota_support import T0, Clock, SqlKit, account

assert collection_models  # метаданные сборщика регистрируются только импортом (см. conftest)

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL не задан: живого Postgres нет",
)

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


async def setup(
    db_engine: AsyncEngine, listings: int
) -> tuple[SqlKit, int, QuotaService, list[int]]:
    kit = SqlKit(db_engine)
    user = await kit.new_user()
    return kit, user, QuotaService(kit.ledger, clock=Clock(T0)), await kit.listings(listings)


# ── гонки: ровно столько, сколько положено ──────────────────────────────────


async def test_twenty_reservations_of_six_cards_at_a_remainder_of_ten_take_exactly_ten(
    db_engine: AsyncEngine,
) -> None:
    """Двадцать ответов одного клиента разом: блокировка периода ставит их в очередь."""
    kit, user, quota, ids = await setup(db_engine, 120)
    who = account(user)

    admissions = await asyncio.gather(
        *(quota.admit(who, ids[start : start + 6]) for start in range(0, 120, 6))
    )

    granted = [listing for admission in admissions for listing in admission.granted]
    assert len(granted) == 10, "потолок бесплатного периода — десять, а не «примерно десять»"
    assert len(set(granted)) == 10, "ни одна карточка не списана дважды"
    rows = await kit.rows(user)
    assert {row.listing_id for row in rows} == set(granted)
    assert sum(len(admission.withheld) for admission in admissions) == 110


async def test_twenty_identical_reservations_write_each_card_once(db_engine: AsyncEngine) -> None:
    """Двойное нажатие, помноженное на десять: одна и та же выдача двадцать раз подряд."""
    kit, user, quota, ids = await setup(db_engine, 6)
    who = account(user)

    admissions = await asyncio.gather(*(quota.admit(who, ids) for _ in range(20)))

    assert sum(len(admission.granted) for admission in admissions) == 6
    assert sum(len(admission.repeated) for admission in admissions) == 19 * 6
    rows = await kit.rows(user)
    assert [row.times_shown for row in rows] == [20] * 6


async def test_the_anchor_is_set_once_when_the_first_reservations_race(
    db_engine: AsyncEngine,
) -> None:
    kit, user, quota, ids = await setup(db_engine, 10)
    who = account(user)
    moments = [T0 + timedelta(milliseconds=index) for index in range(10)]

    admissions = await asyncio.gather(
        *(quota.admit(who, [ids[index]], now=moments[index]) for index in range(10))
    )

    anchor = await kit.anchor(user)
    assert anchor in moments, "якорь — момент одного из первых списаний, а не среднее"
    assert await kit.period_count(user) == 1
    assert {admission.period_end for admission in admissions} == {add_months(anchor, 1)}
    assert sum(len(admission.granted) for admission in admissions) == 10


async def test_two_reservations_on_both_sides_of_a_boundary_each_get_their_own_period(
    db_engine: AsyncEngine,
) -> None:
    kit, user, quota, ids = await setup(db_engine, 21)
    who = account(user)
    await quota.admit(who, ids[:1])  # якорь стоит заранее: гонка за якорь — другой тест
    edge = add_months(T0, 1)

    before, after = await asyncio.gather(
        quota.admit(who, ids[1:11], now=edge - timedelta(microseconds=1)),
        quota.admit(who, ids[11:21], now=edge),
    )

    assert len(before.granted) == 9, "в старом периоде осталось девять"
    assert len(after.granted) == 10, "в новом — полные десять"
    assert await kit.period_count(user) == 2


# ── барьеры самой базы ──────────────────────────────────────────────────────


async def anchored(kit: SqlKit, user: int, anchor: datetime) -> None:
    """Проставить якорь напрямую, минуя репозиторий: так тест подставляет любые числа."""
    async with kit.sessions() as session, session.begin():
        await session.execute(
            update(models.User).where(models.User.id == user).values(quota_anchor_at=anchor)
        )


def period(user: int, anchor: datetime, number: int) -> models.QuotaPeriod:
    return models.QuotaPeriod(
        user_id=user,
        anchor_at=anchor,
        period_no=number,
        period_start=add_months(anchor, number),
        period_end=add_months(anchor, number + 1),
    )


async def test_a_period_with_the_chained_month_boundary_is_rejected_by_the_check(
    db_engine: AsyncEngine,
) -> None:
    """31 января: период №3 начинается 30 апреля, а не 28-го («+1 месяц» от прошлой границы)."""
    kit = SqlKit(db_engine)
    user, anchor = await kit.new_user(), datetime(2027, 1, 31, 10, tzinfo=UTC)
    await anchored(kit, user, anchor)
    async with kit.sessions() as session, session.begin():
        session.add(period(user, anchor, 2))  # правильная граница проходит

    wrong = models.QuotaPeriod(
        user_id=user,
        anchor_at=anchor,
        period_no=3,
        period_start=datetime(2027, 4, 28, 10, tzinfo=UTC),
        period_end=datetime(2027, 5, 28, 10, tzinfo=UTC),
    )
    with pytest.raises(IntegrityError, match="check constraint"):
        async with kit.sessions() as session, session.begin():
            session.add(wrong)


async def test_a_period_on_the_utc_calendar_instead_of_the_vietnamese_one_is_rejected(
    db_engine: AsyncEngine,
) -> None:
    """31 октября 02:00 по Хошимину = 30 октября 19:00 UTC: месяц по UTC дал бы 1 декабря."""
    kit = SqlKit(db_engine)
    user, anchor = await kit.new_user(), datetime(2026, 10, 30, 19, tzinfo=UTC)
    await anchored(kit, user, anchor)
    utc_style = models.QuotaPeriod(
        user_id=user,
        anchor_at=anchor,
        period_no=1,
        period_start=datetime(2026, 11, 30, 19, tzinfo=UTC),
        period_end=datetime(2026, 12, 30, 19, tzinfo=UTC),
    )
    with pytest.raises(IntegrityError, match="check constraint"):
        async with kit.sessions() as session, session.begin():
            session.add(utc_style)


async def test_the_anchor_cannot_change_once_periods_exist(db_engine: AsyncEngine) -> None:
    kit, user, quota, ids = await setup(db_engine, 1)
    await quota.admit(account(user), ids)

    with pytest.raises(IntegrityError, match="foreign key constraint"):
        await anchored(kit, user, T0 + timedelta(days=1))
    assert await kit.anchor(user) == T0


async def test_two_periods_with_the_same_number_are_refused(db_engine: AsyncEngine) -> None:
    kit = SqlKit(db_engine)
    user, anchor = await kit.new_user(), datetime(2027, 3, 3, 10, tzinfo=UTC)
    await anchored(kit, user, anchor)
    async with kit.sessions() as session, session.begin():
        session.add(period(user, anchor, 0))

    with pytest.raises(IntegrityError, match="unique constraint"):
        async with kit.sessions() as session, session.begin():
            session.add(period(user, anchor, 0))


async def test_a_card_cannot_be_written_twice_into_one_period(db_engine: AsyncEngine) -> None:
    """Последний барьер: даже без блокировки в коде дубль не пройдёт."""
    kit, user, quota, ids = await setup(db_engine, 1)
    admission = await quota.admit(account(user), ids)
    assert admission.ticket is not None

    duplicate = models.OfferView(
        user_id=user, period_id=admission.ticket.period_id, listing_id=ids[0], channel="search"
    )
    with pytest.raises(IntegrityError, match="unique constraint"):
        async with kit.sessions() as session, session.begin():
            session.add(duplicate)


async def test_a_view_cannot_point_at_another_accounts_period(db_engine: AsyncEngine) -> None:
    """Составной ключ (период, клиент): подмена идентификатора в коде не свяжет чужое."""
    kit, user, quota, ids = await setup(db_engine, 2)
    stranger = await kit.new_user()
    admission = await quota.admit(account(user), ids[:1])
    assert admission.ticket is not None

    foreign = models.OfferView(
        user_id=stranger, period_id=admission.ticket.period_id, listing_id=ids[1], channel="search"
    )
    with pytest.raises(IntegrityError, match="foreign key constraint"):
        async with kit.sessions() as session, session.begin():
            session.add(foreign)


async def test_the_channel_is_a_closed_list(db_engine: AsyncEngine) -> None:
    kit, user, quota, ids = await setup(db_engine, 2)
    admission = await quota.admit(account(user), ids[:1])
    assert admission.ticket is not None

    bogus = models.OfferView(
        user_id=user, period_id=admission.ticket.period_id, listing_id=ids[1], channel="telepathy"
    )
    with pytest.raises(IntegrityError, match="check constraint"):
        async with kit.sessions() as session, session.begin():
            session.add(bogus)


async def test_a_shown_card_cannot_be_deleted_so_the_quota_never_comes_back_silently(
    db_engine: AsyncEngine,
) -> None:
    kit, user, quota, ids = await setup(db_engine, 1)
    await quota.admit(account(user), ids)

    with pytest.raises(IntegrityError, match="foreign key constraint"):
        async with kit.sessions() as session, session.begin():
            await session.execute(delete(models.Listing).where(models.Listing.id == ids[0]))


# ── формула границ: база против независимой арифметики на Python ────────────

# Те же якоря, что у исследователя R3 (q42), но по ВЬЕТНАМСКОМУ календарю: каждый
# день трёх времён суток, k = 0..14. Выражение — буквально то, что стоит в CHECK.
FORMULA = """
SELECT (extract(epoch FROM a) * 1000000)::bigint AS anchor_us, k,
       (extract(epoch FROM (((a AT TIME ZONE 'Asia/Ho_Chi_Minh') + k * interval '1 month')
                            AT TIME ZONE 'Asia/Ho_Chi_Minh')) * 1000000)::bigint AS boundary_us
FROM (SELECT (d::date + t)::timestamp AT TIME ZONE 'UTC' AS a
      FROM generate_series(date '2026-12-25', date '2028-03-05', interval '1 day') d,
           unnest(array[time '00:00:00', time '12:34:56.789012', time '23:59:59.999999']) t) x,
     generate_series(0, 14) k
ORDER BY 1, 2
"""


def from_us(microseconds: int) -> datetime:
    return EPOCH + timedelta(microseconds=microseconds)


async def computed_by_the_database(db_engine: AsyncEngine, zone: str) -> list[tuple[int, int, int]]:
    async with db_engine.begin() as connection:
        await connection.execute(text(f"SET LOCAL TimeZone TO '{zone}'"))
        rows = (await connection.execute(text(FORMULA))).all()
    return [(int(a), int(k), int(b)) for a, k, b in rows]


async def test_the_database_formula_agrees_with_the_python_arithmetic(
    db_engine: AsyncEngine,
) -> None:
    rows = await computed_by_the_database(db_engine, "UTC")

    assert len(rows) == 437 * 3 * 15, "сетка якорей должна быть той, что заявлена"
    mismatches = [
        (from_us(anchor), k)
        for anchor, k, boundary in rows
        if add_months(from_us(anchor), k) != from_us(boundary)
    ]
    assert not mismatches, f"расхождений базы с Python: {len(mismatches)}, первые {mismatches[:3]}"


@pytest.mark.parametrize("zone", ["Asia/Ho_Chi_Minh", "America/Los_Angeles"])
async def test_the_session_time_zone_does_not_move_a_single_boundary(
    db_engine: AsyncEngine, zone: str
) -> None:
    """`timestamptz + interval` зависит от пояса сессии; `AT TIME ZONE 'зона'` — нет."""
    assert await computed_by_the_database(db_engine, zone) == await computed_by_the_database(
        db_engine, "UTC"
    )


EDGE_ANCHORS = [
    datetime(2027, 1, 29, 10, tzinfo=UTC),
    datetime(2027, 1, 30, 10, tzinfo=UTC),
    datetime(2027, 1, 31, 10, tzinfo=UTC),
    datetime(2028, 1, 31, 10, tzinfo=UTC),
    datetime(2028, 2, 29, 10, tzinfo=UTC),
    datetime(2026, 8, 31, 10, tzinfo=UTC),
    datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
    datetime(2026, 10, 30, 19, tzinfo=UTC),
    datetime(2027, 3, 31, 0, tzinfo=UTC),
]


async def test_the_check_accepts_every_boundary_python_draws_for_the_edge_anchors(
    db_engine: AsyncEngine,
) -> None:
    """Те же границы, что рисует приложение, проходят CHECK таблицы — для 29/30/31 и 29 февраля."""
    kit = SqlKit(db_engine)
    for anchor in EDGE_ANCHORS:
        user = await kit.new_user()
        await anchored(kit, user, anchor)
        async with kit.sessions() as session, session.begin():
            session.add_all(period(user, anchor, number) for number in range(15))
        assert await kit.period_count(user) == 15, anchor
