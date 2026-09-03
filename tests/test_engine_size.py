"""Объём двигателя: он же защита бюджета от кубиков.

Живой отказ 01.09.2026. «найди мне моцокил 200 кубиков» → бюджет 200000 VND
(семь долларов), ноль находок. Клиент поправил дословно — «не 200000 VND, а
обьем мощность двигателя до 200 кубических сантиметров» — и получил тот же
бюджет второй раз.
"""

from __future__ import annotations

import pytest

from sniffer.search.budget_rules import parse_budget
from sniffer.search.engine_size import (
    listing_cc_values,
    read_engine_cc,
    read_engine_size,
    without_engine_cc,
)
from sniffer.search.intake_rules import parse_query


@pytest.mark.parametrize(
    "text",
    [
        "найди мне моцокил 200 кубиков",
        "мотоцикл 200cc",
        "байк 200 куб см",
        "объём двигателя до 200 кубических сантиметров",
        "скутер 200 см3",
    ],
)
def test_displacement_is_read_however_it_is_written(text: str) -> None:
    """Латиница и кириллица вперемешку — обычное дело в чате Нячанга."""
    assert read_engine_cc(text) == 200


def test_a_price_is_not_a_displacement() -> None:
    assert read_engine_cc("скутер до 200 долларов") is None
    assert read_engine_cc("до 10 млн донгов") is None


def test_an_implausible_number_is_not_an_engine() -> None:
    """5000 кубиков — не двигатель, и вырезать оттуда число нельзя: это цена."""
    assert read_engine_cc("продам за 5000 кубиков") is None
    assert "5000" in without_engine_cc("продам за 5000 кубиков")


def test_cubic_centimetres_never_become_a_budget() -> None:
    """Тот самый живой отказ: 200 кубиков → бюджет 200000 VND."""
    passport = parse_query("найди мне моцокил 200 кубиков. какие есть сейчас")

    assert passport.attributes.get("engine_cc") == 200
    assert passport.budget.max is None, "кубики попали в бюджет — ровно та ошибка"


def test_a_correction_is_heard_the_first_time() -> None:
    """Поправка, которую система не слышит, хуже первой ошибки.

    Первая — недоразумение, вторая — ощущение, что тебя не слушают.
    """
    passport = parse_query(
        "не 200000 VND, а обьем мощность двигателя до 200 кубических сантиметров"
    )

    assert passport.attributes.get("engine_cc") == 200
    assert passport.budget.max is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ищу скутер до 400 долларов", 400.0),
        ("не дороже 10 млн", 10_000_000.0),
        ("не 300 долларов а 500", 500.0),
        ("мне не нужен автомат, до 10 млн", 10_000_000.0),
    ],
)
def test_the_rejection_rule_does_not_eat_real_budgets(text: str, expected: float) -> None:
    """«Не дороже» — это потолок, а не отказ от суммы."""
    assert parse_budget(without_engine_cc(text)).max == expected


def test_displacement_and_budget_live_together() -> None:
    passport = parse_query("байк 150 куб см до 10 млн")

    assert passport.attributes.get("engine_cc") == 150
    assert passport.budget.max == 10_000_000


# ── объём несёт направление, а не только число ──────────────────────────────


@pytest.mark.parametrize(
    ("text", "direction"),
    [
        ("нужен мотоцикл 250 кубиков минимум", "min"),
        ("от 250 куб", "min"),
        ("не меньше 250 cc", "min"),
        ("250+ cc", "min"),
        ("250 куб и выше", "min"),
        ("до 250 кубиков", "max"),
        ("не больше 250 cc", "max"),
        ("250 куб максимум", "max"),
        ("250 кубиков", "exact"),
        ("около 250 cc", "exact"),
    ],
)
def test_the_displacement_carries_a_direction(text: str, direction: str) -> None:
    """«от/минимум/250+» — нижняя граница, «до/максимум» — верхняя, иначе точка."""
    size = read_engine_size(text)

    assert size is not None
    assert size.value == 250
    assert size.direction == direction


def test_a_minimum_lands_in_the_passport() -> None:
    """«250 кубиков минимум» несёт в паспорт и число, и направление."""
    passport = parse_query("нужен мотоцикл 250 кубиков минимум")

    assert passport.attributes.get("engine_cc") == 250
    assert passport.attributes.get("engine_cc_dir") == "min"
    assert passport.budget.max is None, "минимум объёма — не бюджет"


def test_a_plain_displacement_carries_no_direction_field() -> None:
    """Точка — прежнее поведение: направление в паспорт не пишется вовсе."""
    passport = parse_query("мотоцикл 200cc")

    assert passport.attributes.get("engine_cc") == 200
    assert "engine_cc_dir" not in passport.attributes


# ── объём ЛОТА читается и голым числом, но не из года и не из цены ───────────


def test_a_bare_number_is_read_as_displacement() -> None:
    """Лоты пишут «nvx 125», «ATTILA 124», «155 abs» — без слова «cc»."""
    assert listing_cc_values("yamaha nvx 125 2017 год") == [125]
    assert listing_cc_values("SYM ATTILAVTS 124 отличный") == [124]
    assert listing_cc_values("155 abs, кофр") == [155]


def test_a_year_is_not_a_displacement() -> None:
    """«Honda Lead 2008» — год выпуска, а не 2008 cc: 2008 вне диапазона объёма."""
    assert listing_cc_values("Honda Lead 2008, пробег 20к") == []


def test_a_price_is_not_a_displacement_in_a_listing() -> None:
    """Цена рядом с единицей или группой разрядов объёмом не читается."""
    assert listing_cc_values("отдам за 10 млн, срочно") == []
    assert listing_cc_values("цена 125.000 донгов") == []
    assert listing_cc_values("500к за байк") == []
    assert listing_cc_values("$150 и торг") == []


def test_a_mileage_interval_is_not_a_displacement() -> None:
    """«масло менялось каждые 1000 км» — расстояние, а не 1000 cc."""
    assert listing_cc_values("CBR150R, масло каждые 1000 км") == [150]


def test_an_explicit_cc_is_still_read() -> None:
    """Явное «250cc» читается как было — голое число его не отменяет."""
    assert listing_cc_values("Kawasaki 250cc, механика") == [250]
