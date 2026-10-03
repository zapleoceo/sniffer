"""Факты на боевых текстах: доля заполнения не падает ниже зафиксированной.

Фикстура `fixtures/listing_facts_corpus.jsonl` — 145 боевых постов из выборки 03.10.2026
(жильё, байки, дананские объявления, меню AN-HOME), с телефонами, ссылками и строками
контактов, вынутыми при сборке. Тест нужен, чтобы деградацию словаря ловил CI: правка
слов или регулярного выражения, после которой «балкон» перестаёт находиться, не уронит ни
один случай-пример, зато уронит долю.

Нижние границы — примерно две трети замеренного на этой выборке, а не сама цифра: выборка
мала, и граница, равная цифре, краснела бы от одного чужого текста в ней. Замеры на всех
18 868 карточках архива — в docs/architecture.md, 5.0.3.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sniffer.domain.districts import PLACE_BY_SLUG
from sniffer.pipeline.listing_facts import FactColumns, fact_columns

CORPUS = Path(__file__).parent / "fixtures" / "listing_facts_corpus.jsonl"
HOUSING = {"apartment", "house", "room"}


def load() -> list[tuple[dict[str, str], FactColumns]]:
    rows = [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines()]
    return [
        (row, fact_columns(row["text"], category=row["cat"], deal_type=row["deal"])) for row in rows
    ]


@pytest.fixture(scope="module")
def cards() -> list[tuple[dict[str, str], FactColumns]]:
    return load()


def share(cards: list[tuple[dict[str, str], FactColumns]], key: str) -> float:
    if key == "district":
        return sum(card.district is not None for _, card in cards) / len(cards)
    if key == "lang":
        return sum(card.lang is not None for _, card in cards) / len(cards)
    return sum(key in card.attributes for _, card in cards) / len(cards)


# Ключ → нижняя граница доли у жилья (замер на выборке в комментарии).
HOUSING_FLOORS = {
    "lang": 1.0,
    "district": 0.60,  # 80%
    "zone": 0.45,  # 64%
    "area_m2": 0.40,  # 60%
    "floor": 0.08,  # 17%
    "elevator": 0.08,  # 15%
    "balcony": 0.18,  # 30%
    "pool": 0.06,  # 12%
    "washing_machine": 0.25,  # 38%
    "pets_allowed": 0.09,  # 17%
    "deposit_months": 0.40,  # 60%
    "min_term_months": 0.33,  # 49%
    "sea_distance_min": 0.05,  # 10%
}
BIKE_FLOORS = {
    "lang": 1.0,
    "year": 0.45,  # 68%
    "mileage_km": 0.12,  # 22%
    "papers": 0.22,  # 38%
    "no_license_claimed": 0.15,  # 25%
    "zone": 0.10,  # 20%
}


@pytest.mark.parametrize(("key", "floor"), HOUSING_FLOORS.items())
def test_a_housing_fact_keeps_its_share_of_the_corpus(
    cards: list[tuple[dict[str, str], FactColumns]], key: str, floor: float
) -> None:
    housing = [(row, card) for row, card in cards if row["cat"] in HOUSING]

    assert share(housing, key) >= floor, f"{key}: {share(housing, key):.0%} < {floor:.0%}"


@pytest.mark.parametrize(("key", "floor"), BIKE_FLOORS.items())
def test_a_bike_fact_keeps_its_share_of_the_corpus(
    cards: list[tuple[dict[str, str], FactColumns]], key: str, floor: float
) -> None:
    bikes = [(row, card) for row, card in cards if row["cat"] == "motorbike"]

    assert share(bikes, key) >= floor, f"{key}: {share(bikes, key):.0%} < {floor:.0%}"


def test_titles_stop_being_junk(cards: list[tuple[dict[str, str], FactColumns]]) -> None:
    """R3: короче 8 знаков или с решётки — мусор; было 15% карточек, цель — меньше 1%."""
    junk = [
        card.title for _, card in cards if len(card.title.strip()) < 8 or card.title.startswith("#")
    ]

    assert len(junk) / len(cards) <= 0.03, junk
    assert all(not re.fullmatch(r"AN-HOME|LVCC|OCEANUS", card.title) for _, card in cards)


def test_every_value_is_inside_the_limits_of_plausibility(
    cards: list[tuple[dict[str, str], FactColumns]],
) -> None:
    """Границы правдоподобия — часть договора: 3 тысячи м² и 99-летний контракт — не факты."""
    limits = {
        "area_m2": (8, 1000),
        "floor": (1, 60),
        "floors_total": (1, 60),
        "deposit_months": (0, 24),
        "min_term_months": (1, 60),
        "sea_distance_min": (1, 90),
        "sea_distance_m": (1, 20_000),
        "year": (1985, 2030),
        "mileage_km": (1, 300_000),
    }
    for _, card in cards:
        for key, (low, high) in limits.items():
            if key in card.attributes:
                value = card.attributes[key]
                assert isinstance(value, int | float) and not isinstance(value, bool), key
                assert low <= value <= high, (key, value)


def test_every_value_has_the_type_the_card_promises(
    cards: list[tuple[dict[str, str], FactColumns]],
) -> None:
    flags = {"balcony", "elevator", "pool", "gym", "washing_machine", "air_conditioner"}
    for _, card in cards:
        for key in flags & set(card.attributes):
            assert isinstance(card.attributes[key], bool), key
        assert card.attributes.get("kitchen", "separate") in {"separate", "shared"}
        assert card.attributes.get("papers", "blue_card") in {"blue_card", "none"}
        assert card.attributes.get("bargain", "fixed") in {"fixed", "negotiable"}
        assert card.attributes.get("zone", "north") in {"north", "center", "south", "west"}
        assert card.district is None or card.district in PLACE_BY_SLUG
        assert card.lang in {"ru", "en", "vi"}
        assert 0 < len(card.title) <= 80 or card.title == "Объявление"
