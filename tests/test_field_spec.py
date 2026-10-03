"""Реестр полей редактора согласован с паспортом и говорит правду о мониторе."""

from __future__ import annotations

import pytest

from sniffer.domain.field_spec import (
    REGISTRY,
    Kind,
    Monitor,
    spec_by_key,
    specs_for,
    uncovered_attributes,
)
from sniffer.domain.passport import CATEGORY_ATTRIBUTES, Category


def test_every_category_attribute_has_a_field() -> None:
    """Атрибут паспорта без поля реестра нельзя ни показать чипом, ни снять."""
    assert uncovered_attributes() == set()


def test_a_field_exists_only_for_categories_that_know_the_attribute() -> None:
    """Поле «Бассейн» у мотобайка — условие, которого паспорт не читает."""
    for spec in REGISTRY:
        if spec.attribute is None or spec.key == "power":
            continue
        for category in spec.categories or ():
            assert spec.attribute in CATEGORY_ATTRIBUTES[category], (spec.key, category)


def test_keys_are_unique() -> None:
    keys = [spec.key for spec in REGISTRY]
    assert len(keys) == len(set(keys))


def test_the_longest_callback_fits_into_64_bytes() -> None:
    from sniffer.bot.filter_card import FilterCallback

    longest = max(REGISTRY, key=lambda s: len(s.key))
    packed = FilterCallback(root=9_999_999_999, v=99_999, f=longest.key, a="s", o="bathroom").pack()
    assert len(packed.encode()) <= 64


def test_choice_fields_have_options_and_the_rest_do_not() -> None:
    for spec in REGISTRY:
        assert bool(spec.options) == (spec.kind is Kind.CHOICE), spec.key


def test_city_cannot_be_cleared_and_nothing_else_is_required() -> None:
    assert [s.key for s in REGISTRY if s.required] == ["city"]


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("city", Monitor.YES),
        ("model", Monitor.YES),
        ("budget_max", Monitor.PARTIAL),
        ("engine_cc", Monitor.PARTIAL),
        ("districts", Monitor.NO),
        ("must_have", Monitor.NO),
        ("deal_breakers", Monitor.NO),
        ("budget_min", Monitor.NO),
    ],
)
def test_the_monitor_flag_follows_the_r6_table(key: str, expected: Monitor) -> None:
    spec = spec_by_key(key)
    assert spec is not None
    assert spec.monitor is expected


def test_fields_for_a_category_include_its_own_attributes_only() -> None:
    bike = {s.key for s in specs_for(Category.MOTORBIKE)}
    flat = {s.key for s in specs_for(Category.APARTMENT)}
    assert {"transmission", "engine_cc", "city"} <= bike
    assert "pool" not in bike
    assert {"pool", "rooms", "city"} <= flat
    assert "transmission" not in flat
    # Категории без атрибутов (велосипед) остаются с общими условиями.
    assert {s.key for s in specs_for(Category.BICYCLE)} == {
        s.key for s in REGISTRY if s.categories is None
    }
