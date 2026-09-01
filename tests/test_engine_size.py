"""Объём двигателя: он же защита бюджета от кубиков.

Живой отказ 01.09.2026. «найди мне моцокил 200 кубиков» → бюджет 200000 VND
(семь долларов), ноль находок. Клиент поправил дословно — «не 200000 VND, а
обьем мощность двигателя до 200 кубических сантиметров» — и получил тот же
бюджет второй раз.
"""

from __future__ import annotations

import pytest

from sniffer.search.budget_rules import parse_budget
from sniffer.search.engine_size import read_engine_cc, without_engine_cc
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
