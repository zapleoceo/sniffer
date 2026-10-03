"""Правка паспорта по реестру: набор изменений даёт ровно одну новую версию без побочных потерь."""

from __future__ import annotations

import pytest

from sniffer.domain.field_spec import spec_by_key
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.domain.passport_edit import REMOVE, Change, EditError, apply_changes, read


def bike(**attributes: object) -> Passport:
    return Passport(
        intent=Intent.BUY,
        category=Category.MOTORBIKE,
        city="nha_trang",
        budget=Budget(max=15_000_000, currency=Currency.VND),
        attributes={"brand": "honda", "model": "lead", "transmission": "automatic", **attributes},
        raw_query="скутер honda lead до 15 млн",
        districts=["Loc Tho"],
    )


def test_a_choice_is_set_and_the_rest_stays_untouched() -> None:
    before = bike()
    after, payload = apply_changes(before, [Change("transmission", "manual")])
    assert after.attributes["transmission"] == "manual"
    assert after.attributes["brand"] == "honda"
    assert after.budget == before.budget and after.raw_query == before.raw_query
    assert payload == {"set": {"transmission": "manual"}, "removed": []}


def test_the_input_passport_is_not_mutated() -> None:
    before = bike()
    apply_changes(before, [Change("brand"), Change("budget_max")])
    assert before.attributes["brand"] == "honda" and before.budget.max == 15_000_000


def test_a_field_can_be_removed_which_merge_edit_could_not() -> None:
    after, payload = apply_changes(bike(), [Change("brand")])
    assert "brand" not in after.attributes
    assert after.attributes["model"] == "lead"
    assert payload == {"set": {}, "removed": ["brand"]}


def test_removing_the_budget_keeps_the_currency_and_period() -> None:
    after, _ = apply_changes(bike(), [Change("budget_max")])
    assert after.budget.max is None
    assert after.budget.currency is Currency.VND


def test_removing_the_engine_size_takes_its_direction_along() -> None:
    before = bike(engine_cc=150, engine_cc_dir="min")
    after, _ = apply_changes(before, [Change("engine_cc")])
    assert "engine_cc" not in after.attributes and "engine_cc_dir" not in after.attributes


def test_a_list_field_is_cleared_to_an_empty_list_not_none() -> None:
    after, _ = apply_changes(bike(), [Change("districts")])
    assert after.districts == []


def test_several_changes_make_one_passport_and_one_payload() -> None:
    after, payload = apply_changes(
        bike(), [Change("transmission", "semi"), Change("brand"), Change("budget_max", 20_000_000)]
    )
    assert (after.attributes["transmission"], after.budget.max) == ("semi", 20_000_000)
    assert payload == {
        "set": {"transmission": "semi", "budget_max": 20_000_000},
        "removed": ["brand"],
    }


@pytest.mark.parametrize(
    "change",
    [
        Change("transmission", "tiptronic"),  # такого варианта нет
        Change("budget_max", -5),
        Change("budget_max", True),  # bool — не число, хотя isinstance(True, int)
        Change("budget_max", "много"),
        Change("model", "  "),
        Change("districts", []),
        Change("pool", True),  # поле жилья у мотобайка
        Change("no_such_field", 1),
        Change("city"),  # без города монитор не работает: снять нельзя
    ],
)
def test_a_bad_change_is_refused(change: Change) -> None:
    with pytest.raises(EditError):
        apply_changes(bike(), [change])


def test_a_flag_wants_a_boolean() -> None:
    flat = Passport(intent=Intent.RENT, category=Category.APARTMENT, city="nha_trang")
    after, _ = apply_changes(flat, [Change("pool", True)])
    assert after.attributes["pool"] is True
    with pytest.raises(EditError):
        apply_changes(flat, [Change("pool", "yes")])


def test_an_empty_edit_is_refused() -> None:
    with pytest.raises(EditError):
        apply_changes(bike(), [])


def test_nothing_is_applied_when_a_later_change_is_bad() -> None:
    """Набор — одна правка: полуприменённого паспорта наружу не выходит."""
    before = bike()
    with pytest.raises(EditError):
        apply_changes(before, [Change("brand"), Change("transmission", "tiptronic")])
    assert before.attributes["brand"] == "honda"


def test_remove_is_a_sentinel_distinct_from_none() -> None:
    assert Change("brand").value is REMOVE
    assert Change("brand").removes
    assert not Change("brand", None).removes


def test_read_returns_the_value_by_path() -> None:
    spec = spec_by_key("budget_max")
    assert spec is not None
    assert read(bike(), spec) == 15_000_000
    assert read(bike(), spec_by_key("districts")) == ["Loc Tho"]  # type: ignore[arg-type]
    assert read(Passport(), spec) is None
