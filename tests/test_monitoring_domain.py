"""Чистая часть слота слежения: сутки по Вьетнаму, отбор под потолок, «ещё N», порядок слотов."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest

from sniffer.domain.monitoring import (
    HEAD_JUMP_AFTER_HOURS,
    Overflow,
    add_overflow,
    local_day,
    local_day_start,
    must_jump_to_head,
    open_slot_ids,
    pick,
)
from sniffer.domain.plans import MONITOR_CARDS_PER_DAY
from sniffer.domain.records import SubscriptionState

# Время зашито намеренно: чистая функция получает момент аргументом (см. test_db_clock_rule).
NOON_UTC = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Card:
    id: int | None


def test_the_daily_cap_is_ten_and_new_slots_get_it_by_default() -> None:
    assert MONITOR_CARDS_PER_DAY == 10
    assert SubscriptionState.__dataclass_fields__["max_per_day"].default == 10


def test_the_day_starts_at_vietnamese_midnight_not_utc_midnight() -> None:
    # 17:30 UTC — уже 00:30 следующих суток по Вьетнаму: потолок обнулился.
    late = datetime(2026, 10, 3, 17, 30, tzinfo=UTC)
    assert local_day(late) == date(2026, 10, 4)
    assert local_day_start(late) == datetime(2026, 10, 3, 17, 0, tzinfo=UTC)


def test_just_before_vietnamese_midnight_is_still_the_old_day() -> None:
    before = datetime(2026, 10, 3, 16, 59, 59, tzinfo=UTC)
    assert local_day(before) == date(2026, 10, 3)
    assert local_day_start(before) == datetime(2026, 10, 2, 17, 0, tzinfo=UTC)


def test_the_old_utc_boundary_does_not_reset_the_cap() -> None:
    # Полночь UTC — это 07:00 клиента: сутки в этот момент не меняются.
    midnight_utc = datetime(2026, 10, 3, 0, 0, tzinfo=UTC)
    assert local_day_start(midnight_utc) == datetime(2026, 10, 2, 17, 0, tzinfo=UTC)


def test_under_the_cap_the_newest_are_taken_and_delivered_in_posting_order() -> None:
    result = pick([Card(1), Card(2), Card(3), Card(4), Card(5)], room=2)
    assert [card.id for card in result.chosen] == [4, 5], "новейшие, а не старейшие (D1)"
    assert result.overflow == 3


def test_everything_fits_when_the_room_is_large_enough() -> None:
    result = pick([Card(7), Card(3)], room=10)
    assert [card.id for card in result.chosen] == [3, 7]
    assert result.overflow == 0


@pytest.mark.parametrize("room", [0, -4])
def test_no_room_means_everything_overflows(room: int) -> None:
    result = pick([Card(1), Card(2)], room=room)
    assert result.chosen == []
    assert result.overflow == 2


def test_nothing_to_pick_is_not_an_overflow() -> None:
    nothing: list[Card] = []
    result = pick(nothing, room=10)
    assert result.chosen == [] and result.overflow == 0


TODAY = date(2026, 10, 4)


def test_the_first_overflow_of_the_day_asks_for_a_summary() -> None:
    state, notify = add_overflow(Overflow(), today=TODAY, extra=3)
    assert state == Overflow(TODAY, 3, True)
    assert notify is True


def test_a_later_overflow_of_the_same_day_only_refines_the_number() -> None:
    state, notify = add_overflow(Overflow(TODAY, 3, True), today=TODAY, extra=4)
    assert state == Overflow(TODAY, 7, True)
    assert notify is False, "сводка раз в сутки, а не на каждый проход"


def test_a_new_day_starts_the_count_over_and_asks_again() -> None:
    state, notify = add_overflow(Overflow(TODAY, 9, True), today=TODAY + timedelta(days=1), extra=2)
    assert state == Overflow(TODAY + timedelta(days=1), 2, True)
    assert notify is True


def test_no_overflow_changes_nothing_but_the_day() -> None:
    state, notify = add_overflow(Overflow(TODAY, 5, True), today=TODAY, extra=0)
    assert state == Overflow(TODAY, 5, True) and notify is False
    rolled, notify = add_overflow(
        Overflow(TODAY, 5, True), today=TODAY + timedelta(days=1), extra=0
    )
    assert rolled == Overflow(TODAY + timedelta(days=1), 0, False) and notify is False


def test_only_the_first_slots_of_a_client_work() -> None:
    assert open_slot_ids([11, 12, 13], 1) == {11}
    assert open_slot_ids([11, 12, 13], 2) == {11, 12}


def test_unknown_slot_count_means_no_restriction() -> None:
    assert open_slot_ids([11, 12], None) == {11, 12}


@pytest.mark.parametrize("slots", [0, -1])
def test_no_slots_stops_everything_without_deleting_anything(slots: int) -> None:
    assert open_slot_ids([11, 12], slots) == set()


def test_more_slots_than_monitors_is_fine() -> None:
    assert open_slot_ids([11], 5) == {11}


def test_a_short_pause_keeps_the_cursor_and_a_long_one_jumps() -> None:
    limit = timedelta(hours=HEAD_JUMP_AFTER_HOURS)
    assert must_jump_to_head(NOON_UTC - limit + timedelta(minutes=1), NOON_UTC) is False
    assert must_jump_to_head(NOON_UTC - limit - timedelta(minutes=1), NOON_UTC) is True
    assert must_jump_to_head(None, NOON_UTC) is False


def test_a_negative_room_takes_nothing_instead_of_slicing_from_the_end() -> None:
    # `ordered[:-1]` — не «ничего», а «всё, кроме последней»: потолок не бывает отрицательным.
    result = pick([Card(1), Card(2), Card(3), Card(4), Card(5)], room=-1)
    assert result.chosen == [] and result.overflow == 5
