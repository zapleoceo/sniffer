"""Цена в проходе догона: политика замены накопленного значения.

Политика записана в `pipeline/enrich_price.py` и в `docs/architecture.md`
(5.0.4), и у каждой её строки здесь свой тест: NULL заполняется; плохое старое
значение заменяется только подтверждённым меткой; всё, что могло быть выбрано
осознанно, не трогается, а лишь считается; найденное «ничего» не стирает
имеющееся. Тексты — реальные шаблоны объявлений, номера и @username выдуманы.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sniffer.domain.listing_patch import ListingPatch
from sniffer.domain.price_bounds import price_bounds
from sniffer.pipeline.enrich_price import (
    ABSENT,
    DISAGREED,
    FILLED,
    KEPT_IMPLAUSIBLE,
    LOST,
    LOST_IMPLAUSIBLE,
    RATE_ONLY,
    REPLACED,
    SAME,
    derive_price,
    judge,
)
from sniffer.pipeline.listing_price import PriceColumns
from tests.enrich_support import card

# Квартира в аренду: нижняя и верхняя граница здравого смысла, донги в месяц.
BOUNDS = price_bounds("apartment", "rent_out")
assert BOUNDS == (2_000_000, 150_000_000), "тесты ниже написаны под эти границы"

SOURCES = ["label", "weak", "money", "text", "footer"]
NOT_LABELLED = ["weak", "money", "text", "footer"]
DAILY = PriceColumns(attributes={"rate_amount": 250_000, "rate_currency": "VND", "rate_per": "day"})


def monthly(amount: int) -> PriceColumns:
    return PriceColumns(Decimal(amount), "VND", "month")


# ── политика, строка за строкой ────────────────────────────────────────────


@pytest.mark.parametrize("source", SOURCES)
def test_an_empty_price_is_filled_by_whatever_the_text_confirms(source: str) -> None:
    assert judge(None, monthly(9_000_000), source=source, bounds=BOUNDS) == FILLED


def test_an_implausible_old_price_is_replaced_by_a_labelled_plausible_one() -> None:
    """«5500» вместо «5,5 млн»: ошибка прежнего разбора, ради которой проход и нужен."""
    assert judge(Decimal(5_500), monthly(5_500_000), source="label", bounds=BOUNDS) == REPLACED


@pytest.mark.parametrize("source", NOT_LABELLED)
def test_an_implausible_old_price_is_not_replaced_by_a_weaker_confirmation(source: str) -> None:
    """Метка — единственное подтверждение, которому проход доверяет против имеющегося."""
    verdict = judge(Decimal(5_500), monthly(5_500_000), source=source, bounds=BOUNDS)

    assert verdict == KEPT_IMPLAUSIBLE


def test_a_labelled_price_outside_the_bounds_does_not_replace_either() -> None:
    """Замена обязана улучшать: новое значение вне границ — не лучше плохого старого."""
    verdict = judge(Decimal(5_500), monthly(900_000_000), source="label", bounds=BOUNDS)

    assert verdict == KEPT_IMPLAUSIBLE


@pytest.mark.parametrize("source", SOURCES)
def test_two_plausible_prices_that_differ_are_counted_never_changed(source: str) -> None:
    """Выбор наименьшей цены каталога вместо первой мог быть осознанным — не трогаем.

    И метка тут ничего не меняет: правило «метка заменяет» касается лишь
    значений, которые вне границ.
    """
    verdict = judge(Decimal(7_000_000), monthly(9_000_000), source=source, bounds=BOUNDS)

    assert verdict == DISAGREED


def test_the_same_price_is_the_same() -> None:
    assert judge(Decimal("9000000.00"), monthly(9_000_000), source="label", bounds=BOUNDS) == SAME


def test_without_bounds_nothing_is_implausible_so_nothing_is_replaced() -> None:
    """Для пары без границ «вне границ» не определено: старое значение остаётся."""
    assert price_bounds("room", "sell") is None

    verdict = judge(Decimal(5_500), monthly(5_500_000), source="label", bounds=None)

    assert verdict == DISAGREED


def test_a_price_that_was_there_is_never_erased() -> None:
    assert judge(Decimal(9_000_000), PriceColumns(), source=None, bounds=BOUNDS) == LOST


def test_an_implausible_price_that_was_there_is_not_erased_either_but_is_told_apart() -> None:
    """Таких в архиве десятки (5 млрд за аренду дома): видно в отчёте, но не стирается."""
    verdict = judge(Decimal(5_000_000_000), PriceColumns(), source=None, bounds=BOUNDS)

    assert verdict == LOST_IMPLAUSIBLE


def test_nothing_before_and_nothing_found_is_absent() -> None:
    assert judge(None, PriceColumns(), source=None, bounds=BOUNDS) == ABSENT


def test_a_daily_rate_before_nothing_is_told_apart_from_no_price() -> None:
    assert judge(None, DAILY, source="text", bounds=BOUNDS) == RATE_ONLY


def test_a_daily_rate_never_replaces_a_stored_price() -> None:
    """Суточное и доллары идут в атрибуты; колонка остаётся, как была."""
    assert judge(Decimal(9_000_000), DAILY, source="text", bounds=BOUNDS) == LOST
    assert judge(Decimal(280_000), DAILY, source="text", bounds=BOUNDS) == LOST_IMPLAUSIBLE


@pytest.mark.parametrize(
    ("old", "kept"),
    [
        (BOUNDS[0], True),
        (BOUNDS[1], True),
        (BOUNDS[0] - 1, False),
        (BOUNDS[1] + 1, False),
    ],
)
def test_the_bounds_themselves_are_plausible(old: int, kept: bool) -> None:
    """Границы включительно: ровно 2 млн — ещё аренда, 1 999 999 — уже нет."""
    verdict = judge(Decimal(old), monthly(9_000_000), source="label", bounds=BOUNDS)

    assert verdict == (DISAGREED if kept else REPLACED)


# ── вывод целиком: текст → патч ────────────────────────────────────────────


def test_an_empty_price_is_filled_with_dong_and_the_period_of_the_deal() -> None:
    patch = derive_price(card(), "Oceanus, 2 спальни.\nАрендная плата: 12.5 млн VND / месяц")

    assert dict(patch.columns) == {
        "price_amount": Decimal(12_500_000),
        "price_currency": "VND",
        "price_period": "month",
    }
    assert patch.outcomes == (FILLED,)


def test_a_sale_is_priced_once_not_monthly() -> None:
    patch = derive_price(
        card(category="motorbike", deal_type="sell"), "Продам Honda Vision 2019. Цена 21 млн"
    )

    assert patch.columns["price_period"] == "once"
    assert patch.columns["price_amount"] == Decimal(21_000_000)


def test_a_replacement_writes_only_what_actually_differs() -> None:
    """Валюта и срок у «5500» уже верные — перезаписывается одна сумма."""
    patch = derive_price(card(price=5_500), "Сдаётся 1-комн. квартира.\nЦена: 5,5 млн VND/мес")

    assert dict(patch.columns) == {"price_amount": Decimal(5_500_000)}
    assert patch.outcomes == (REPLACED,)


def test_a_bare_number_in_thousands_is_read_by_the_bounds_of_the_category() -> None:
    """«Цена 40000» у квартиры — это 40 млн: так записано и в докс, и в базе (`40000`)."""
    patch = derive_price(card(price=40_000), "Сдам квартиру. Цена 40000")

    assert patch.columns["price_amount"] == Decimal(40_000_000)
    assert patch.outcomes == (REPLACED,)


def test_a_disagreement_changes_nothing_at_all() -> None:
    patch = derive_price(card(price=7_000_000), "Сдам квартиру 2 спальни, 9 000 000 ₫/мес")

    assert patch.is_empty
    assert patch.outcomes == (DISAGREED,)


def test_a_text_with_no_price_leaves_the_card_alone() -> None:
    patch = derive_price(card(price=9_000_000), "Продам Honda Vision 2019")

    assert patch.is_empty and patch.outcomes == (LOST,)
    assert derive_price(card(), "Продам Honda Vision 2019").outcomes == (ABSENT,)


def test_a_daily_price_goes_to_the_attributes_and_the_column_stays_empty() -> None:
    patch = derive_price(card(category="motorbike"), "Honda Vision в аренду, 250 000 в сутки")

    assert patch.columns == {}
    assert dict(patch.attributes) == {
        "rate_amount": 250_000,
        "rate_currency": "VND",
        "rate_per": "day",
    }
    assert patch.outcomes == (RATE_ONLY,)


def test_dollars_go_to_the_attributes_too_and_never_pose_as_dong() -> None:
    patch = derive_price(card(), "Сдам студию, 400 USD в месяц")

    assert patch.columns == {}
    assert patch.attributes["rate_currency"] == "USD"


def test_a_rate_next_to_a_stored_price_is_added_without_touching_the_price() -> None:
    patch = derive_price(
        card(category="motorbike", price=200_000), "Honda Vision в аренду, 250 000 в сутки"
    )

    assert patch.columns == {}, "price_amount не трогаем"
    assert patch.attributes["rate_amount"] == 250_000
    assert patch.outcomes == (LOST_IMPLAUSIBLE,)


# ── атрибуты цены следуют за колонкой ──────────────────────────────────────

RANGE = "Сдам квартиру. Цена: от 9 до 11 млн/мес"


def test_the_upper_bound_of_a_range_comes_with_a_filled_price() -> None:
    patch = derive_price(card(), RANGE)

    assert patch.columns["price_amount"] == Decimal(9_000_000)
    assert dict(patch.attributes) == {"price_up_to": 11_000_000}


def test_the_upper_bound_is_not_written_beside_a_price_that_was_kept() -> None:
    """Верх вилки новой цены рядом со старой (7 млн) дал бы «7 млн — до 11 млн».

    Это две разные цены, склеенные в одну, а не диапазон из объявления.
    """
    patch = derive_price(card(price=7_000_000), RANGE)

    assert patch.is_empty, "расхождение: ни колонка, ни атрибут не меняются"
    assert patch.outcomes == (DISAGREED,)


def test_the_upper_bound_is_added_when_the_price_itself_already_matches() -> None:
    patch = derive_price(card(price=9_000_000), RANGE)

    assert patch.columns == {}
    assert dict(patch.attributes) == {"price_up_to": 11_000_000}
    assert patch.outcomes == (SAME,)


def test_attributes_that_are_already_right_are_not_written_again() -> None:
    patch = derive_price(card(price=9_000_000, attributes={"price_up_to": 11_000_000}), RANGE)

    assert patch.is_empty


def test_other_attributes_of_the_card_are_not_the_price_derivations_business() -> None:
    patch = derive_price(
        card(attributes={"rooms": 2, "furnished": True}), "Oceanus.\nАрендная плата: 12 млн/мес"
    )

    assert set(patch.attributes) <= {"price_up_to", "rate_amount", "rate_currency", "rate_per"}


# ── идемпотентность ────────────────────────────────────────────────────────

CASES = [
    ("apartment", None, "Oceanus, 2 спальни.\nАрендная плата: 12.5 млн VND / месяц"),
    ("apartment", 5_500, "Сдаётся 1-комн. квартира.\nЦена: 5,5 млн VND/мес"),
    ("apartment", 40_000, "Сдам квартиру. Цена 40000"),
    ("apartment", None, RANGE),
    ("apartment", 9_000_000, RANGE),
    ("apartment", 7_000_000, RANGE),
    ("motorbike", None, "Honda Vision в аренду, 250 000 в сутки"),
    ("motorbike", 200_000, "Honda Vision в аренду, 250 000 в сутки"),
    ("apartment", None, "Сдам студию, 400 USD в месяц"),
    ("apartment", 9_000_000, "Продам Honda Vision 2019"),
    ("apartment", None, "Продам Honda Vision 2019"),
    ("apartment", 7_000_000, "Сдам квартиру 2 спальни, 9 000 000 ₫/мес"),
]


@pytest.mark.parametrize(("category", "price", "text"), CASES)
def test_a_second_pass_over_the_same_text_changes_nothing(
    category: str, price: int | None, text: str
) -> None:
    before = card(category=category, price=price)
    first = derive_price(before, text)

    after = first.applied_to(before)
    second = derive_price(after, text)

    assert second.is_empty, "повторный проход ничего не пишет"
    assert second.applied_to(after) == after


@pytest.mark.parametrize(("category", "price", "text"), CASES)
def test_the_patch_stays_inside_the_price_columns(
    category: str, price: int | None, text: str
) -> None:
    patch = derive_price(card(category=category, price=price), text)

    assert set(patch.columns) <= {"price_amount", "price_currency", "price_period"}
    assert isinstance(patch, ListingPatch)
