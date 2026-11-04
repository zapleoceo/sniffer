"""Допись под выдачей уточняет ту же ветку, пока предмет и город не сменились."""

from __future__ import annotations

import pytest

from sniffer.domain.passport import Category
from sniffer.search.intake_rules import parse_query
from sniffer.search.refinements import refine, refines

CURRENT = parse_query("ищу скутер в Нячанге")


@pytest.mark.parametrize("text", ["honda lead", "автомат", "в Нячанге", "до 300 долларов"])
def test_an_addendum_with_the_same_subject_refines(text: str) -> None:
    assert refines(CURRENT, parse_query(text))


@pytest.mark.parametrize(
    "text", ["ищу квартиру в Нячанге", "ищу скутер в Дананге", "привет", "ищу лодку"]
)
def test_another_subject_another_city_or_nothing_recognised_is_not_a_refinement(
    text: str,
) -> None:
    assert not refines(CURRENT, parse_query(text))


def test_a_branch_without_a_category_is_never_refined() -> None:
    assert not refines(parse_query("привет"), parse_query("honda lead"))


def test_refine_keeps_the_old_conditions_and_adds_the_new_ones() -> None:
    revised = refine(
        CURRENT, parse_query("honda lead до 300 долларов"), "honda lead до 300 долларов"
    )

    assert revised.city == CURRENT.city and revised.category == CURRENT.category
    assert revised.attributes["model"] == "lead" and revised.budget.max == 300
    assert revised.raw_query.startswith("ищу скутер в Нячанге")


def test_a_restated_subject_with_a_place_the_dictionary_does_not_know_is_a_new_search() -> None:
    assert not refines(CURRENT, parse_query("ищу скутер в куангнгае"))


def test_northern_nha_trang_edit_keeps_the_existing_housing_search() -> None:
    current = parse_query("сниму квартиру в Нячанге до 500 долларов, 2 комнаты")
    text = "Измени только район на северный район Нячанга"
    fresh = parse_query(text)

    assert fresh.districts
    assert "vinh_hai" in fresh.districts
    assert "vinh_phuoc" in fresh.districts
    assert refines(current, fresh)
    revised = refine(current, fresh, text)
    assert revised.category == current.category
    assert revised.city == current.city
    assert revised.budget == current.budget
    assert revised.attributes == current.attributes
    assert revised.districts == fresh.districts


def test_northern_housing_zone_is_not_applied_to_another_city_or_vehicle() -> None:
    assert not parse_query("квартира в Дананге, северный район").districts
    assert not parse_query("скутер на севере Нячанга").districts


def test_later_budget_edit_preserves_the_selected_districts() -> None:
    current = parse_query("сниму квартиру на севере Нячанга")
    fresh = parse_query("до 600 долларов")

    revised = refine(current, fresh, "до 600 долларов")
    assert revised.districts == current.districts
    assert revised.budget.max == 600


def test_owner_northern_house_request_keeps_house_rooms_and_district() -> None:
    request = parse_query("2 комнаты минимум / Дом / Нячанг / Район север / До 10 млн")

    assert request.category == Category.HOUSE
    assert request.city == "nha_trang"
    assert request.attributes["rooms"] == 2
    assert request.budget.max == 10_000_000
    assert "vinh_hai" in request.districts
    assert "vinh_phuoc" in request.districts


def test_room_in_house_is_still_a_room_and_orientation_is_not_a_district() -> None:
    request = parse_query("сниму комнату в доме в Нячанге, окна на север")

    assert request.category == Category.ROOM
    assert request.districts == []
    assert not parse_query("дом в Дананге, район север").districts
    assert not parse_query("дом в Нячанге, район северо-запад").districts
