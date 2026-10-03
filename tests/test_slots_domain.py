"""Слоты мониторинга: раскладка сроков, решение «Следить», перенос слота. Чистая арифметика.

Правила из `docs/monetization.md`: слот — оплаченный срок подписки Stars; мониторингов может
быть больше, чем слотов, и тогда лишние ждут, ничего не теряя; продление возобновляет их само.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sniffer.domain.slots import (
    Monitor,
    Outcome,
    SlotState,
    assign_expiry,
    decide_enable,
    live_ends,
    plan_move,
    state_after,
)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def monitor(
    ident: int, *, priority: int = 0, expires: datetime | None = NOW + 10 * DAY, active: bool = True
) -> Monitor:
    return Monitor(
        id=ident, root=ident * 100, priority=priority, expires_at=expires, is_active=active
    )


# ── сроки живых подписок ────────────────────────────────────────────────────


def test_only_live_terms_count_and_the_longest_comes_first() -> None:
    ends = [NOW + 3 * DAY, NOW - DAY, NOW + 20 * DAY, NOW]

    assert live_ends(ends, NOW) == [NOW + 20 * DAY, NOW + 3 * DAY]


def test_a_term_that_ends_exactly_now_gives_no_slot() -> None:
    assert live_ends([NOW], NOW) == []


# ── раскладка ───────────────────────────────────────────────────────────────


def test_the_senior_monitor_gets_the_longest_term() -> None:
    first, second = monitor(1, priority=0, expires=NOW), monitor(2, priority=1, expires=NOW)
    ends = [NOW + 30 * DAY, NOW + 5 * DAY]

    assert assign_expiry(ends, [second, first], NOW) == {1: ends[0], 2: ends[1]}


def test_the_order_of_claims_is_priority_first_and_the_id_only_breaks_ties() -> None:
    """Перенос слота — смена `priority`, а не `id`: у более нового мониторинга порядок выше."""
    old_but_junior = monitor(1, priority=5, expires=NOW)
    new_but_senior = monitor(2, priority=0, expires=NOW)
    ends = [NOW + 30 * DAY, NOW + 5 * DAY]

    changes = assign_expiry(ends, [old_but_junior, new_but_senior], NOW)

    assert changes == {2: ends[0], 1: ends[1]}


def test_equal_priorities_fall_back_to_the_older_monitor_first() -> None:
    first, second = monitor(1, priority=0, expires=NOW), monitor(2, priority=0, expires=NOW)
    ends = [NOW + 30 * DAY, NOW + 5 * DAY]

    assert assign_expiry(ends, [second, first], NOW) == {1: ends[0], 2: ends[1]}


def test_a_monitor_without_a_term_is_left_without_a_slot_and_nothing_is_deleted() -> None:
    held = monitor(1, expires=NOW + 5 * DAY)
    waiting = monitor(2, priority=1, expires=NOW + 5 * DAY)

    changes = assign_expiry([NOW + 5 * DAY], [held, waiting], NOW)

    assert changes == {2: NOW}
    assert 1 not in changes


def test_a_lapsed_monitor_keeps_its_old_date_instead_of_being_rewritten() -> None:
    lapsed = monitor(1, expires=NOW - 3 * DAY)

    assert assign_expiry([], [lapsed], NOW) == {}


def test_a_renewal_brings_a_lapsed_monitor_back() -> None:
    lapsed = monitor(1, expires=NOW - 3 * DAY)

    state, changes = state_after([NOW + 30 * DAY], [lapsed], NOW)

    assert changes == {1: NOW + 30 * DAY}
    assert state == SlotState(slots=1, holding=1, resumed=1)


def test_a_manual_grant_without_a_term_is_not_a_slot_and_is_not_touched() -> None:
    manual = monitor(1, expires=None)
    paid = monitor(2, priority=1, expires=NOW)

    changes = assign_expiry([NOW + 9 * DAY], [manual, paid], NOW)

    assert changes == {2: NOW + 9 * DAY}


def test_the_junior_monitor_drops_out_when_the_nearest_subscription_ends() -> None:
    """Две подписки, два мониторинга; короткая кончилась — слот теряет младший, а не старший."""
    senior = monitor(1, priority=0, expires=NOW + 30 * DAY)
    junior = monitor(2, priority=1, expires=NOW + 2 * DAY)
    later = NOW + 3 * DAY

    state, changes = state_after(live_ends([NOW + 33 * DAY], later), [senior, junior], later)

    assert changes == {1: NOW + 33 * DAY}, "старший получил срок продлённой подписки"
    assert 2 not in changes, "младший просрочен сам по дате, переписывать нечего"
    assert state.slots == 1 and state.holding == 1


def test_the_state_counts_free_slots() -> None:
    state, _ = state_after([NOW + 5 * DAY, NOW + 6 * DAY], [monitor(1, expires=NOW + DAY)], NOW)

    assert state.slots == 2 and state.holding == 1 and state.free == 1


def test_a_refund_shortens_the_term_at_once() -> None:
    """Возврат убирает подписку из журнала: слот гаснет сразу, а не в конце срока."""
    held = monitor(1, expires=NOW + 20 * DAY)

    state, changes = state_after([], [held], NOW)

    assert changes == {1: NOW} and state.holding == 0 and state.slots == 0


# ── «Следить» ───────────────────────────────────────────────────────────────


def test_without_any_paid_subscription_the_answer_is_to_subscribe() -> None:
    assert decide_enable(0, [], 500, NOW) is Outcome.NEEDS_SUBSCRIPTION


def test_a_free_slot_lets_a_new_branch_be_enabled() -> None:
    assert decide_enable(1, [], 500, NOW) is Outcome.ENABLE


def test_when_every_slot_is_taken_a_new_branch_cannot_be_enabled() -> None:
    assert decide_enable(1, [monitor(1)], 500, NOW) is Outcome.NO_FREE_SLOT


def test_a_branch_that_already_holds_a_slot_is_already_on() -> None:
    assert decide_enable(1, [monitor(1)], 100, NOW) is Outcome.ALREADY_ON


def test_a_user_paused_branch_with_a_slot_is_resumed_not_refused() -> None:
    assert decide_enable(1, [monitor(1, active=False)], 100, NOW) is Outcome.ENABLE


def test_a_lapsed_branch_with_no_subscription_needs_one() -> None:
    assert decide_enable(0, [monitor(1, expires=NOW - DAY)], 100, NOW) is Outcome.NEEDS_SUBSCRIPTION


def test_a_lapsed_branch_while_the_slot_sits_on_another_is_refused() -> None:
    held, lapsed = monitor(1), monitor(2, priority=1, expires=NOW - DAY)

    assert decide_enable(1, [held, lapsed], 200, NOW) is Outcome.NO_FREE_SLOT


def test_a_term_ending_exactly_now_does_not_hold_a_slot() -> None:
    assert decide_enable(1, [monitor(1, expires=NOW)], 500, NOW) is Outcome.ENABLE


# ── перенос ─────────────────────────────────────────────────────────────────


def test_a_move_promotes_the_target_and_demotes_the_source() -> None:
    source, target = monitor(1, priority=0), monitor(2, priority=1, expires=NOW - DAY)

    plan = plan_move([source, target], to_root=200, from_root=100, now=NOW)

    assert plan is not None
    assert (plan.demote_id, plan.promote_id) == (1, 2)
    assert plan.demote_to > 1 > 0 > plan.promote_to


def test_a_move_to_a_branch_with_no_monitor_yet_asks_to_create_one() -> None:
    plan = plan_move([monitor(1)], to_root=999, from_root=100, now=NOW)

    assert plan is not None and plan.promote_id is None


@pytest.mark.parametrize(
    ("monitors", "to_root", "from_root"),
    [
        ([monitor(1, expires=NOW - DAY)], 999, 100),  # слот с источника уже не оплачен
        ([monitor(1)], 999, 777),  # источника нет вовсе
        ([monitor(1), monitor(2, priority=1)], 200, 100),  # цель уже держит слот
        ([monitor(1)], 100, 100),  # та же ветка
    ],
    ids=["source_unpaid", "no_source", "target_holds", "same_branch"],
)
def test_an_impossible_move_is_refused(
    monitors: list[Monitor], to_root: int, from_root: int
) -> None:
    assert plan_move(monitors, to_root=to_root, from_root=from_root, now=NOW) is None


def test_a_paused_or_archived_monitor_does_not_hold_a_slot_and_the_next_one_gets_it() -> None:
    paused = monitor(1, priority=0, active=False)
    waiting = monitor(2, priority=1, expires=NOW - DAY)
    ends = [NOW + 20 * DAY]

    changes = assign_expiry(ends, [paused, waiting], NOW)

    assert changes[1] == NOW  # пауза слот отдала
    assert changes[2] == NOW + 20 * DAY  # и он достался следующему по порядку


def test_state_after_does_not_count_a_paused_monitor_as_holding() -> None:
    paused = monitor(1, active=False)
    state, _ = state_after([NOW + 20 * DAY], [paused], NOW)
    assert (state.slots, state.holding) == (1, 0)


def test_a_paused_monitor_that_was_never_paid_keeps_its_history() -> None:
    past = NOW - 3 * DAY
    assert assign_expiry([NOW + DAY], [monitor(1, expires=past, active=False)], NOW) == {}
