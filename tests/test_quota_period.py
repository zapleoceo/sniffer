"""Периоды квоты: границы считаются точно, от якоря и в UTC.

Требование владельца к счётчику бесплатных карточек — «нужна точность расчёта».
Поэтому тут не только примеры, но и сверка со вторым, независимо написанным
вычислением на тысячах случайных пар «якорь, сейчас».
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from sniffer.domain.quota_period import VIETNAM, Period, add_months, period_containing


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        ("2027-01-31T10:00:00", 1, "2027-02-28T10:00:00"),
        ("2027-01-31T10:00:00", 2, "2027-03-31T10:00:00"),
        ("2027-01-31T10:00:00", 3, "2027-04-30T10:00:00"),
        ("2028-01-31T10:00:00", 1, "2028-02-29T10:00:00"),
        ("2028-02-29T00:00:00", 12, "2029-02-28T00:00:00"),
        ("2028-02-29T00:00:00", 48, "2032-02-29T00:00:00"),
        ("2026-12-15T23:59:59", 1, "2027-01-15T23:59:59"),
        ("2026-11-30T08:00:00", 3, "2027-02-28T08:00:00"),
        ("2027-03-31T08:00:00", -1, "2027-02-28T08:00:00"),
        ("2026-10-17T09:30:00", 0, "2026-10-17T09:30:00"),
    ],
)
def test_a_month_is_a_calendar_month_with_the_day_clamped(
    start: str, months: int, expected: str
) -> None:
    assert add_months(at(start), months) == at(expected)


def test_the_time_of_day_and_microseconds_are_kept() -> None:
    moment = datetime(2026, 10, 17, 9, 30, 15, 123456, tzinfo=UTC)

    assert add_months(moment, 1) == datetime(2026, 11, 17, 9, 30, 15, 123456, tzinfo=UTC)


def test_every_boundary_is_counted_from_the_anchor_not_from_the_previous_one() -> None:
    """31 января → 28 февраля → 31 марта, а не → 28 марта: обрезка не копится."""
    anchor = at("2027-01-31T10:00:00")
    ends = [add_months(anchor, k) for k in range(1, 6)]

    assert [end.strftime("%m-%d") for end in ends] == ["02-28", "03-31", "04-30", "05-31", "06-30"]


def test_the_period_of_the_anchor_itself_starts_at_the_anchor() -> None:
    anchor = at("2026-10-17T09:30:00")

    assert period_containing(anchor, anchor) == Period(anchor, at("2026-11-17T09:30:00"))


def test_the_last_instant_belongs_to_the_period_and_the_end_to_the_next() -> None:
    anchor = at("2026-10-17T09:30:00")
    end = at("2026-11-17T09:30:00")

    assert period_containing(anchor, end - timedelta(microseconds=1)).end == end
    assert period_containing(anchor, end) == Period(end, at("2026-12-17T09:30:00"))


def test_a_comeback_after_a_long_silence_gets_the_period_that_contains_now() -> None:
    anchor = at("2026-10-17T09:30:00")

    period = period_containing(anchor, at("2027-01-05T12:00:00"))

    assert period == Period(at("2026-12-17T09:30:00"), at("2027-01-17T09:30:00"))


def test_a_short_month_period_after_the_anchor_on_the_31st() -> None:
    anchor = at("2027-01-31T10:00:00")

    assert period_containing(anchor, at("2027-03-01T00:00:00")) == Period(
        at("2027-02-28T10:00:00"), at("2027-03-31T10:00:00")
    )


def test_a_moment_before_the_anchor_is_the_first_period_not_a_crash() -> None:
    """Часы двух процессов расходятся на миллисекунды: квота не должна падать."""
    anchor = at("2026-10-17T09:30:00")

    assert period_containing(anchor, anchor - timedelta(milliseconds=5)).start == anchor


def test_a_naive_moment_is_a_bug_of_the_caller_and_fails_loudly() -> None:
    with pytest.raises(ValueError, match="часовым поясом"):
        period_containing(datetime(2026, 10, 17, 9, 30), at("2026-10-18T00:00:00"))
    with pytest.raises(ValueError, match="часовым поясом"):
        add_months(datetime(2026, 10, 17, 9, 30), 1)


def test_a_local_time_zone_is_converted_to_utc_before_the_boundary_is_drawn() -> None:
    """Якорь 00:30 по Хошимину — это 17:30 UTC накануне; граница считается по UTC."""
    ho_chi_minh = timezone(timedelta(hours=7))
    anchor = datetime(2026, 10, 17, 0, 30, tzinfo=ho_chi_minh)

    period = period_containing(anchor, anchor)

    assert period.start == at("2026-10-16T17:30:00")
    assert period.end == at("2026-11-16T17:30:00")
    assert period.start.tzinfo is UTC


def test_contains_is_half_open() -> None:
    period = Period(at("2026-10-17T00:00:00"), at("2026-11-17T00:00:00"))

    assert period.contains(period.start)
    assert not period.contains(period.end)


def test_the_calendar_is_vietnamese_so_a_night_anchor_does_not_slip_a_day() -> None:
    """31 октября в 2 часа ночи по Хошимину — это 30 октября 19:00 UTC.

    Месяц спустя клиент ждёт 30 ноября (обрезка 31 → 30). Арифметика по UTC
    выдала бы 30 ноября 19:00 UTC, то есть 1 декабря по местному, и «месяц»
    оказался бы длиннее на день.
    """
    anchor = at("2026-10-30T19:00:00")

    local = period_containing(anchor, anchor).end.astimezone(VIETNAM)
    naive_utc = period_containing(anchor, anchor, UTC).end.astimezone(VIETNAM)

    assert local.date() == date(2026, 11, 30)
    assert naive_utc.date() == date(2026, 12, 1)
    # Сдвиг Вьетнама закреплён абсолютным моментом, а не тем же `VIETNAM`, что и в коде.
    assert period_containing(anchor, anchor).end == at("2026-11-29T19:00:00")
    # Ровно тот случай, где UTC+7 и UTC+8 расходятся на день: 30 октября 23:30 против 31 октября 00:30.
    eve = at("2026-10-30T16:30:00")
    assert period_containing(eve, eve).end == at("2026-11-30T16:30:00")


def reference_days_in_month(year: int, month: int) -> int:
    """Длина месяца без `calendar`: через разность первых чисел."""
    first = datetime(year, month, 1, tzinfo=UTC)
    following = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=UTC)
    return (following - first).days


def reference_add(anchor: datetime, months: int, zone: timezone) -> datetime:
    """Шаг за шагом, месяц за месяцем, по местному календарю: второй способ."""
    local = anchor.astimezone(zone)
    year, month = local.year, local.month
    for _ in range(months):
        month += 1
        if month == 13:
            year, month = year + 1, 1
    moved = local.replace(
        year=year, month=month, day=min(local.day, reference_days_in_month(year, month))
    )
    return moved.astimezone(UTC)


def reference_period(anchor: datetime, now: datetime, zone: timezone) -> tuple[datetime, datetime]:
    index = 0
    while reference_add(anchor, index + 1, zone) <= now:
        index += 1
    return reference_add(anchor, index, zone), reference_add(anchor, index + 1, zone)


@pytest.mark.parametrize("zone", [VIETNAM, UTC, timezone(timedelta(hours=-5))], ids=str)
def test_the_periods_agree_with_an_independent_stepwise_calculation(zone: timezone) -> None:
    generator = random.Random(20261003)  # noqa: S311 -- воспроизводимая выборка, не секрет
    base = datetime(2024, 1, 1, tzinfo=UTC)
    for _ in range(3000):
        anchor = base + timedelta(seconds=generator.randrange(3 * 365 * 86_400))
        now = anchor + timedelta(seconds=generator.randrange(2 * 365 * 86_400))

        period = period_containing(anchor, now, zone)

        assert (period.start, period.end) == reference_period(anchor, now, zone), (anchor, now)
        assert period.contains(now)
        assert 28 <= (period.end - period.start).days <= 31


@pytest.mark.parametrize("zone", [VIETNAM, UTC], ids=str)
def test_consecutive_periods_touch_without_gaps_or_overlaps(zone: timezone) -> None:
    anchor = at("2027-01-31T23:59:59")
    period = period_containing(anchor, anchor, zone)
    for _ in range(60):
        following = period_containing(anchor, period.end, zone)

        assert following.start == period.end
        assert period_containing(anchor, period.end - timedelta(microseconds=1), zone) == period
        period = following
