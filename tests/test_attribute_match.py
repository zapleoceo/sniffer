"""Числа паспорта и карточки сравниваются по смыслу, а не как строки."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sniffer.domain.passport import Category, Intent, Passport
from sniffer.domain.records import Listing
from sniffer.matching.attribute_match import as_number, conflicts, matches
from sniffer.matching.rules import score, worth_sending

NOW = datetime(2026, 10, 4, tzinfo=UTC)


@pytest.mark.parametrize(
    ("field", "actual", "wanted", "same"),
    [
        ("area_m2", 64.5, 60, True),
        ("area_m2", 69, 60, True),
        ("area_m2", 70, 60, False),
        ("area_m2", 51, 60, True),
        ("area_m2", 50, 60, False),
        ("area_m2", "65.0", "65", True),
        ("deposit_months", 1.5, 1, True),
        ("deposit_months", 2, 1, False),
        ("deposit_months", 1, 1.5, True),
        ("min_term_months", 3, 3.0, True),
        ("min_term_months", 6, 3, False),
        ("rooms", 2, 2.0, True),
        ("rooms", 3, 2, False),
        ("floor", 5, 5, True),
        ("floor", 6, 5, False),
        ("brand", "Honda", "honda", True),
        ("brand", "yamaha", "honda", False),
        ("furnished", True, True, True),
        ("furnished", False, True, False),
    ],
)
def test_the_same_value_by_meaning(field: str, actual: object, wanted: object, same: bool) -> None:
    assert matches(field, actual, wanted, {field: wanted}) is same


def test_the_engine_band_is_not_this_modules_business() -> None:
    """Полосу объёма держит `matching.rules`; здесь объём — обычное число, а ветки нет."""
    import inspect

    assert "engine_cc" not in inspect.getsource(matches)


@pytest.mark.parametrize("actual", [None, ""])
def test_an_unnamed_value_is_not_a_conflict(actual: object) -> None:
    assert not conflicts("area_m2", actual, 60, {})


def test_a_named_value_that_differs_is_a_conflict() -> None:
    assert conflicts("area_m2", 120, 60, {})
    assert not conflicts("area_m2", 61, 60, {})


def test_a_bool_is_not_a_quantity() -> None:
    assert as_number(True) is None
    assert as_number("abc") is None
    assert as_number(None) is None
    assert as_number("1,5") == 1.5
    assert not matches("elevator", True, 1, {})


def _flat(**attributes: object) -> Listing:
    return Listing(
        raw_message_id=1,
        deal_type="rent_out",
        category="apartment",
        city="nha_trang",
        title="Квартира",
        summary="",
        tg_link="https://t.me/c/1/1",
        posted_at=NOW,
        attributes=attributes,
    )


def _wish(**attributes: object) -> Passport:
    return Passport(
        intent=Intent.RENT, category=Category.APARTMENT, city="nha_trang", attributes=attributes
    )


def test_a_slightly_bigger_flat_is_still_sent_and_a_far_one_is_not() -> None:
    wish = _wish(area_m2=60)

    assert worth_sending(_flat(area_m2=64.5), wish, now=NOW)
    assert not worth_sending(_flat(area_m2=90), wish, now=NOW)


def test_an_equal_number_in_another_spelling_scores_as_a_match() -> None:
    wish = _wish(area_m2=65, rooms=2)

    assert score(_flat(area_m2=65.0, rooms=2.0), wish, now=NOW) == score(
        _flat(area_m2=65, rooms=2), wish, now=NOW
    )
    assert score(_flat(area_m2=65.0, rooms=2.0), wish, now=NOW) > score(
        _flat(area_m2=120, rooms=5), wish, now=NOW
    )
