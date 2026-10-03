"""Допись под выдачей уточняет ту же ветку, пока предмет и город не сменились."""

from __future__ import annotations

import pytest

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
