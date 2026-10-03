"""Бюджет клиента читает те же единицы суммы, что и цена в объявлении.

Раньше у бюджета был свой короткий список («к», «млн», «tr»…), и «до 15кк», «до 2 tỷ»
и «до 20🍋» читались как 15, 2 и 20 долларов — а «цена 15кк» в объявлении при этом
читалась как 15 миллионов донгов. Одно знание — «как пишут миллион» — жило в двух местах.
"""

from __future__ import annotations

import pytest

from sniffer.domain.passport import Currency
from sniffer.domain.price_numbers import factor
from sniffer.domain.price_vocab import UNIT_FACTORS
from sniffer.search.budget_rules import parse_budget

# «м» — метры, «ml» — миллилитры: в объявлении рядом с суммой они читаются деньгами
# («36 m» одной строкой), а в речи клиента («400 м от моря») нет.
NOT_A_BUDGET_UNIT = {"м", "ml", "мил", "mil"}


@pytest.mark.parametrize(
    ("text", "maximum"),
    [
        ("сниму квартиру до 15кк", 15_000_000),
        ("куплю квартиру до 2 tỷ", 2_000_000_000),
        ("куплю дом до 1,5 млрд", 1_500_000_000),
        ("байк до 20🍋", 20_000_000),
        ("квартира до 10 млн", 10_000_000),
        ("скутер до 300к", 300_000),
        ("квартира до 10tr", 10_000_000),
        ("квартира до 10 triệu", 10_000_000),
        ("квартира до 800 nghìn", 800_000),
    ],
)
def test_the_budget_understands_every_way_listings_write_a_million(text: str, maximum: int) -> None:
    budget = parse_budget(text)

    assert budget.max == maximum
    assert budget.currency is Currency.VND


@pytest.mark.parametrize(
    "name", [name for name, _ in UNIT_FACTORS if name not in NOT_A_BUDGET_UNIT]
)
def test_every_unit_of_the_price_vocabulary_is_a_unit_of_the_budget(name: str) -> None:
    """Связь, а не список: новая единица в `price_vocab` сразу понятна и бюджету."""
    assert parse_budget(f"до 5{name}").max == 5 * factor(name)


def test_metres_are_not_millions_in_a_client_phrase() -> None:
    budget = parse_budget("квартира в 400 м от моря, до 10 млн")

    assert budget.max == 10_000_000


def test_miles_are_not_millions_in_a_client_phrase() -> None:
    assert parse_budget("до 5 миль от моря, до 400$").max == 400
    assert parse_budget("квартира до 5 миллионов").max == 5_000_000


@pytest.mark.parametrize("text", ["скутер 2021", "honda 2019", "2021", "скутер 2021 уехал"])
def test_a_bare_year_is_not_a_budget(text: str) -> None:
    """Резерв при молчащей модели читал «скутер 2021» как «до 2021 $» и резал выдачу."""
    assert parse_budget(text).max is None


@pytest.mark.parametrize(
    ("text", "maximum"),
    [
        ("скутер до 2021", 2021),
        ("скутер 2021$", 2021),
        ("скутер $2021", 2021),
        ("скутер 2021 долларов", 2021),
        ("скутер 2021 usd", 2021),
        ("квартира 2000-2500$", 2500),
    ],
)
def test_four_digits_stay_a_budget_when_the_client_said_so(text: str, maximum: int) -> None:
    assert parse_budget(text).max == maximum


def test_a_range_of_four_digit_prices_keeps_both_edges() -> None:
    budget = parse_budget("квартира 2000-2500$")

    assert (budget.min, budget.max) == (2000, 2500)
