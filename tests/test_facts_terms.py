"""Залог, срок договора и расстояние до моря: числа и их соседи.

Каждый случай — реальный шаблон из чатов (11 936 постов с залогом, 03.10.2026), сокращённый
до строк, на которых он держится. В посте три числа подряд — «депозит 1 · оплата 1 ·
контракт от 3 месяцев», — и каждое принадлежит своему слову.
"""

from __future__ import annotations

import pytest

from sniffer.domain.facts_sea import read_sea_distance
from sniffer.domain.facts_terms import (
    read_deposit_amount,
    read_deposit_months,
    read_min_term_months,
)
from sniffer.domain.facts_text import fact_text


def deposit(text: str) -> int | float | None:
    return read_deposit_months(fact_text(text))


def term(text: str) -> int | float | None:
    return read_min_term_months(fact_text(text))


def sea(text: str) -> dict[str, int]:
    return read_sea_distance(fact_text(text))


def case(name: str, text: str, expected: object) -> object:
    return pytest.param(text, expected, id=name)


DEPOSITS = [
    case("label_and_months", "🔒 Депозит: 1 месяц", 1),
    case("label_and_abbreviation", "VND / мес (депозит 1 мес, контракт 3–6 мес)", 1),
    case("for_a_month", "Депозит за 1 месяц, оплата за 1 месяц", 1),
    case("a_word_instead_of_a_digit", "залог за один месяц и арендная плата за один месяц", 1),
    case("months_of_deposit_first", "💵 2 месяца залога – 1 месяц оплаты", 2),
    case("bare_numbers", "Депозит 2 оплата 2", 2),
    case("two_plus_one", "(депозит 1+1)", 1),
    case("english_pair", "Deposit 1, pay 1, flexible contract for 3-6 months", 1),
    case("english_typo", "Pay 3 month, deposti 2", 2),
    case("english_hyphen", "1-month deposit – 1-month payment", 1),
    case("viet_deposit", "Giá : 35 triệu/ tháng ( cọc 2 thanh toán 2)", 2),
    case("deposit_equals_a_monthly_rent", "💘 депозит = аренда за 1 месяц, возвращается", 1),
    case("deposit_equals_the_monthly_cost", "🔰депозит равен месячной стоимости", 1),
    case("deposit_is_the_monthly_payment", "депозит в размере суммы месячного платежа", 1),
    case("half_a_month_more", "Залог 1,5 месяца", 1.5),
    case("no_deposit", "Аренда без залога", 0),
    case("no_deposit_english", "No deposit needed", 0),
    case("first_one_wins", "Залог: 1 месяц\nDeposit: 2 months", 1),
    case(
        "the_term_line_above_is_not_the_deposit",
        "контракт от 12 мес\nдепозит 2 мес оплата 3 мес",
        2,
    ),
    case("the_contract_next_to_a_deposit_label", "договор 6 месяцев\nзалог: 1 месяц", 1),
]
NOT_DEPOSITS = [
    case("an_amount_in_millions", "♻️ Депозит: 18 млн", None),
    case("an_amount_in_dots", "залог 5.000.000 VND", None),
    case("an_amount_in_thousands", "залог 500к", None),
    case("a_big_bare_number", "депозит 10", None),
    case("too_many_months", "залог 30 месяцев", None),
    case("a_small_amount_in_millions", "Депозит 5 млн", None),
    case("a_small_amount_viet", "Tiền cọc 3 triệu", None),
    case("payment_is_not_a_deposit", "оплата 3 месяца вперёд", None),
    case("silence", "Квартира у моря, 10 млн", None),
]


@pytest.mark.parametrize(("text", "expected"), DEPOSITS + NOT_DEPOSITS)
def test_the_deposit_is_read_in_months_or_left_unsaid(
    text: str, expected: int | float | None
) -> None:
    assert deposit(text) == expected


def amount(text: str) -> int | None:
    return read_deposit_amount(fact_text(text))


AMOUNTS = [
    case("millions", "♻️ Депозит: 18 млн", 18_000_000),
    case("fraction_of_a_million", "Залог 1,5 млн", 1_500_000),
    case("dots", "залог 5.000.000 VND", 5_000_000),
    case("spaces", "Залог: 12 000 000 донг", 12_000_000),
    case("thousands", "залог 500к", 500_000),
    case("viet", "Tiền cọc 3 triệu", 3_000_000),
    case("viet_with_word_deposit", "deposit 3tr", 3_000_000),
    case("english", "Security deposit: 10 million VND", 10_000_000),
    case("attached_m", "Deposit 18M", 18_000_000),
    case("a_spaced_m_is_not_millions", "deposit 2 m", None),
    case("for_the_amount", "залог в размере 20 млн", 20_000_000),
    case("months_are_not_an_amount", "Депозит 1 месяц", None),
    case("a_bare_number_is_not_an_amount", "депозит 10", None),
    case("a_bare_small_number", "залог 2", None),
    case("lower_bound_is_included", "залог 100 тыс", 100_000),
    case("upper_bound_is_included", "залог 200 млн", 200_000_000),
    case("a_word_starting_with_tr_is_not_a_unit", "deposit 3 trung tam", None),
    case("too_little", "залог 5 тыс", None),
    case("too_much", "залог 900 млн", None),
    case("no_deposit", "Аренда без залога", None),
    case("silence", "Квартира у моря, 10 млн", None),
    case("first_one_wins", "Залог 10 млн" + chr(10) + "Deposit 12 млн", 10_000_000),
]


@pytest.mark.parametrize(("text", "expected"), AMOUNTS)
def test_the_deposit_named_by_a_sum_is_read_in_dong(text: str, expected: int | None) -> None:
    assert amount(text) == expected


TERMS = [
    case("label_from_months", "• Контракт: от 3 месяцев", 3),
    case("label_months", "Срок договора: 6 месяцев", 6),
    case("range_takes_the_lower", "Договор — 3–6 месяцев", 3),
    case("a_triple_range", "Договор: 3 – 6 – 12 месяцев", 3),
    case("a_year", "Контракт на 1 год", 12),
    case("from_a_year", "Контракт: от 1 года", 12),
    case("rent_from_a_word", "аренда от одного месяца", 1),
    case("rent_from_three_months", "аренда от трех месяцев", 3),
    case("english_lease", "📄 Lease: 3–6 months | Deposit: 1 month", 3),
    case("english_list", "Lease terms: 3, 6, or 12 months", 3),
    case("english_more_than", "📋 Contract: more than 3 months", 3),
    case("english_greater_than_a_year", "📋 Contract: > 1 year", 12),
    case("viet_contract", "– hợp đồng: 5 năm", 60),
    case("half_a_year", "Договор от полугода", 6),
    case("the_first_one_wins", "Контракт: от 1 мес (если менее 3 мес, то +1.5 млн)", 1),
    case("the_first_label_wins", "Контракт: от 3 месяцев\nContract: 12 months", 3),
]
NOT_TERMS = [
    case(
        "deposit_after_a_contract_word",
        "Условия договора: депозит 1 месяц ~ оплата за 1 месяц",
        None,
    ),
    case("a_contract_without_a_number", "Долгосрочный контракт, гибкий график", None),
    case("price_after_rent_from", "аренда от 8.000.000 vnd/мес", None),
    case("too_long", "Контракт: 99 лет", None),
]


@pytest.mark.parametrize(("text", "expected"), TERMS + NOT_TERMS)
def test_the_minimum_term_is_read_in_months_or_left_unsaid(
    text: str, expected: int | float | None
) -> None:
    assert term(text) == expected


SEA = [
    case("minutes_to_the_sea", "✅ 5 МИНУТ ДО МОРЯ", {"sea_distance_min": 5}),
    case("walk_to_the_sea", "• ≈ 2 минуты пешком до моря", {"sea_distance_min": 2}),
    case("minutes_walking_from", "Студия в 5 минутах пешком от моря", {"sea_distance_min": 5}),
    case("to_the_beach", "10 мин до пляжа • отдельная спальня", {"sea_distance_min": 10}),
    case("sea_first_minutes_last", "до моря минут 6", {"sea_distance_min": 6}),
    case("meters_to_the_sea", "🌊 Всего 300 метров до моря", {"sea_distance_m": 300}),
    case("meters_from_the_beach", "СДАЁТСЯ СТУДИЯ В 200 МЕТРАХ ОТ ПЛЯЖА", {"sea_distance_m": 200}),
    case("beach_first_meters_last", "до пляжа 50 метров", {"sea_distance_m": 50}),
    case("kilometers", "🌊 До пляжа 4 км", {"sea_distance_m": 4000}),
    case("english_walk", "5-minute walk to the beach", {"sea_distance_min": 5}),
    case("english_meters", "150 m from the beach", {"sea_distance_m": 150}),
    case("viet_minutes", "5 phút đi bộ ra biển", {"sea_distance_min": 5}),
    case(
        "meters_win_over_the_bike",
        "Около 300 м до моря – примерно 3 минуты на байке",
        {"sea_distance_m": 300},
    ),
]
NOT_SEA = [
    case("by_bike_is_another_distance", "• ≈ 10 минут на байке до моря", {}),
    case("by_scooter_after_the_sea", "до моря 7 минут на байке", {}),
    case("by_car_english", "15 minutes by car to the beach", {}),
    case("no_unit", "до моря 50", {}),
    case("no_number", "Рядом с морем, шаговая доступность до пляжа", {}),
    case("too_far_in_minutes", "200 минут до моря", {}),
    case("too_far_in_kilometers", "до пляжа 25 км", {}),
]


@pytest.mark.parametrize(("text", "expected"), SEA + NOT_SEA)
def test_the_distance_to_the_sea_keeps_its_unit(text: str, expected: dict[str, int]) -> None:
    """Минуты и метры не пересчитываются; минуты на байке — вообще другое расстояние."""
    assert sea(text) == expected
