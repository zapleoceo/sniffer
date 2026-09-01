"""Правила подбора: кого берём в подписку и насколько находка хороша."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.domain.records import Listing
from sniffer.matching import MATCH_MIN_SCORE, filter_for, score, worth_sending

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def passport(**overrides: object) -> Passport:
    fields: dict[str, object] = {
        "intent": Intent.BUY,
        "category": Category.MOTORBIKE,
        "city": "nha_trang",
    }
    fields.update(overrides)
    return Passport(**fields)  # type: ignore[arg-type]


def listing(**overrides: object) -> Listing:
    fields: dict[str, object] = {
        "raw_message_id": 1,
        "deal_type": "buy",
        "category": "motorbike",
        "city": "nha_trang",
        "title": "Honda Vision 2021",
        "summary": "Автомат",
        "tg_link": "https://t.me/c/1/1",
        "posted_at": NOW,
    }
    fields.update(overrides)
    return Listing(**fields)  # type: ignore[arg-type]


# ── условия отбора ──────────────────────────────────────────────────────────


def test_a_passport_without_a_city_is_not_a_subscription() -> None:
    """Подписка на всё подряд — это спам, за который бота отключают."""
    assert filter_for(passport(city=None), now=NOW) is None
    assert filter_for(passport(category=None), now=NOW) is None


def test_a_dollar_budget_becomes_a_dong_ceiling() -> None:
    """Объявления написаны в донгах, бюджет клиент назвал в долларах."""
    spec = filter_for(
        passport(budget=Budget(max=300, currency=Currency.USD)), usd_vnd=26000, now=NOW
    )

    assert spec is not None and spec.max_price_vnd == Decimal("7800000")


def test_without_a_rate_the_dollar_budget_does_not_narrow_anything() -> None:
    """Занизить потолок выдуманным курсом хуже, чем не сузить вовсе."""
    spec = filter_for(passport(budget=Budget(max=300, currency=Currency.USD)), now=NOW)

    assert spec is not None and spec.max_price_vnd is None


def test_the_window_of_interest_is_bounded() -> None:
    """Объявление месячной давности в подписку не годится: оно продано."""
    spec = filter_for(passport(), now=NOW)

    assert spec is not None and spec.since is not None
    assert spec.since < NOW


# ── оценка находки ──────────────────────────────────────────────────────────


def test_a_fresh_matching_listing_is_worth_sending() -> None:
    assert worth_sending(listing(), passport(), now=NOW)


def test_a_stale_listing_is_not_worth_waking_a_client_for() -> None:
    """Мы будим клиента сами — вчерашнее объявление плохой повод."""
    old = listing(posted_at=NOW - timedelta(days=13))

    assert score(old, passport(), now=NOW) < MATCH_MIN_SCORE


def test_matching_attributes_score_higher_than_contradicting_ones() -> None:
    wanted = passport(attributes={"transmission": "automatic"})
    good = listing(attributes={"transmission": "automatic"})
    bad = listing(attributes={"transmission": "manual"})

    assert score(good, wanted, now=NOW) > score(bad, wanted, now=NOW)


def test_an_attribute_the_funnel_cannot_extract_yet_is_not_a_penalty() -> None:
    """Минимальная карточка атрибутов не извлекает.

    Штрафовать за то, чего воронка пока не умеет, значит не отправить ни одного
    уведомления до появления полного извлечения.
    """
    wanted = passport(attributes={"transmission": "automatic"})

    assert score(listing(attributes={}), wanted, now=NOW) == score(listing(), passport(), now=NOW)


def test_the_score_never_leaves_zero_to_one() -> None:
    """Оценка — доля, и складывать её с чем-то ещё имеет смысл только так."""
    for candidate in (listing(), listing(posted_at=NOW - timedelta(days=400))):
        assert 0.0 <= score(candidate, passport(), now=NOW) <= 1.0
