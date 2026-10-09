"""Правило повтора отказа — чистая функция, без базы и сети.

Один и тот же `evaluate` стоит и за кнопкой на странице, и за проверкой под замком в
репозитории, поэтому здесь проверяется вся таблица решений: класс, потолок попыток на
ключ, cooldown, FloodWait-стоп, суточный лимит и порядок этих проверок.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sniffer.domain import reject_reasons, reject_retry
from sniffer.domain.reject_reasons import REASONS, RejectClass
from sniffer.domain.reject_retry import RetryCode, RetryFacts, evaluate

# Точка отсчёта для арифметики правила: значение не сверяется с часами базы.
T0 = datetime.fromtimestamp(1_800_000_000, tz=UTC)

FRESH = RetryFacts(
    reason="too_many_attempts",
    now=T0,
    attempts_total=0,
    last_requested_at=None,
    used_in_window=0,
    oldest_in_window=None,
    blocked_until=None,
)


def facts(**changes: object) -> RetryFacts:
    return replace(FRESH, **changes)  # type: ignore[arg-type]


def test_a_fresh_temporary_reject_can_be_retried() -> None:
    decision = evaluate(FRESH)

    assert decision.allowed and decision.code is RetryCode.OK


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("user", RetryCode.PERMANENT),
        ("channel", RetryCode.PERMANENT),
        ("bot", RetryCode.PERMANENT),
        ("foreign_city", RetryCode.PERMANENT),
        ("join_refused", RetryCode.PERMANENT),
        ("already_member", RetryCode.MEMBERSHIP),
        ("already_inside", RetryCode.MEMBERSHIP),
        ("join_request_sent", RetryCode.PENDING),
        ("request_needed", RetryCode.PENDING),
        ("unresolved", RetryCode.UNKNOWN),
        ("city_unknown", RetryCode.UNKNOWN),
        ("code_nobody_heard_of", RetryCode.UNKNOWN),
    ],
)
def test_every_non_temporary_class_is_refused_with_its_own_code(
    reason: str, code: RetryCode
) -> None:
    decision = evaluate(facts(reason=reason))

    assert not decision.allowed and decision.code is code
    assert decision.available_at is None, "«никогда» не притворяется «позже»"


def test_unknown_explains_that_a_missing_chat_cannot_be_told_from_a_failure() -> None:
    assert "не различить отсутствие чата и сбой" in evaluate(facts(reason="unresolved")).message


def test_only_the_temporary_class_is_retryable_and_the_table_agrees_with_the_dictionary() -> None:
    allowed = {reason for reason in REASONS if evaluate(facts(reason=reason)).allowed}

    assert allowed == set(reject_reasons.reasons_of(RejectClass.TEMPORARY)) == {"too_many_attempts"}


def test_the_per_key_ceiling_stops_the_fourth_attempt_even_if_everything_else_is_open() -> None:
    last = T0 - reject_retry.COOLDOWN * 5

    assert evaluate(facts(attempts_total=2, last_requested_at=last)).allowed
    decision = evaluate(
        facts(attempts_total=reject_retry.MAX_RETRIES_PER_KEY, last_requested_at=last)
    )

    assert decision.code is RetryCode.ATTEMPTS_EXHAUSTED


def test_cooldown_blocks_until_exactly_the_cooldown_has_passed() -> None:
    last = T0 - reject_retry.COOLDOWN + timedelta(seconds=1)

    blocked = evaluate(facts(attempts_total=1, last_requested_at=last))
    assert blocked.code is RetryCode.COOLDOWN
    assert blocked.available_at == last + reject_retry.COOLDOWN

    boundary = T0 - reject_retry.COOLDOWN
    assert evaluate(facts(attempts_total=1, last_requested_at=boundary)).allowed


def test_a_flood_stop_blocks_until_it_ends_and_not_after() -> None:
    stop = T0 + timedelta(hours=7)

    decision = evaluate(facts(blocked_until=stop))
    assert decision.code is RetryCode.FLOOD_STOP and decision.available_at == stop

    assert evaluate(facts(blocked_until=T0)).allowed, "стоп, закончившийся сейчас, не держит"
    assert evaluate(facts(blocked_until=T0 - timedelta(hours=1))).allowed


def test_too_many_attempts_is_not_retryable_before_the_flood_stop_ends() -> None:
    """`too_many_attempts` — единственный временный: до конца стопа кнопка выключена."""
    stop = T0 + timedelta(hours=3)

    assert not evaluate(facts(reason="too_many_attempts", blocked_until=stop)).allowed
    assert evaluate(facts(reason="too_many_attempts", now=stop)).allowed


def test_the_daily_limit_opens_when_the_oldest_request_leaves_the_window() -> None:
    oldest = T0 - timedelta(hours=20)

    nine = evaluate(
        facts(used_in_window=reject_retry.MAX_RETRIES_PER_DAY - 1, oldest_in_window=oldest)
    )
    ten = evaluate(facts(used_in_window=reject_retry.MAX_RETRIES_PER_DAY, oldest_in_window=oldest))

    assert nine.allowed
    assert ten.code is RetryCode.DAILY_LIMIT
    assert ten.available_at == oldest + reject_retry.RETRY_WINDOW


def test_the_limits_are_the_documented_numbers() -> None:
    """Числа названы в docs/dashboard.md: смена одного без другого — расхождение."""
    assert reject_retry.MAX_RETRIES_PER_DAY == 10
    assert reject_retry.MAX_RETRIES_PER_KEY == 3
    assert reject_retry.COOLDOWN == timedelta(hours=24)
    assert reject_retry.RETRY_WINDOW == timedelta(hours=24)


def test_a_substantive_refusal_wins_over_the_daily_limit() -> None:
    """429 получает только запрос, который иначе прошёл бы; «нельзя вообще» — это 409."""
    full = facts(used_in_window=reject_retry.MAX_RETRIES_PER_DAY)

    assert evaluate(replace(full, reason="user")).code is RetryCode.PERMANENT
    assert evaluate(replace(full, attempts_total=3)).code is RetryCode.ATTEMPTS_EXHAUSTED
    assert (
        evaluate(replace(full, blocked_until=T0 + timedelta(hours=1))).code is RetryCode.FLOOD_STOP
    )
    assert evaluate(full).code is RetryCode.DAILY_LIMIT


def test_the_next_retry_moment_is_one_cooldown_ahead() -> None:
    assert reject_retry.next_retry_at(T0) == T0 + reject_retry.COOLDOWN
