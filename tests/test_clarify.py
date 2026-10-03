"""Планировщик вопросов: чистая функция, проверяется свойствами, а не примерами."""

from __future__ import annotations

import itertools
import random
from typing import Any

import pytest

from sniffer.domain.clarify import (
    MAX_QUESTIONS,
    Ask,
    ClarificationPlanner,
    Search,
    expected_rest,
)
from sniffer.domain.dialogue import SHOW_ALL, SKIP, DialogueState
from sniffer.domain.facets import Facet, FacetReport, FacetValue, facets_from
from sniffer.domain.passport import Budget, Category, Currency, Passport
from sniffer.sources.base import RawItem

PLANNER = ClarificationPlanner()


def bike_item(n: int, **attrs: Any) -> RawItem:
    return RawItem(
        source="archive",
        external_id=str(n),
        url=f"https://t.me/c/{n}",
        price_vnd=attrs.pop("price", None),
        raw={"listing_id": n, "attributes": attrs},
    )


def market(count: int = 60, *, seed: int = 7) -> list[RawItem]:
    rng = random.Random(seed)  # noqa: S311 — детерминированная выборка теста, не криптография
    brands = ["honda", "honda", "yamaha", "sym", "kymco"]
    models = ["lead", "vision", "air_blade", "pcx", "exciter", "attila"]
    return [
        bike_item(
            i,
            brand=rng.choice(brands),
            **({"model": rng.choice(models)} if rng.random() < 0.8 else {}),
            price=rng.choice([None, 8_000_000, 12_000_000, 20_000_000, 35_000_000]),
        )
        for i in range(count)
    ]


def passport(**attrs: Any) -> Passport:
    return Passport(category=Category.MOTORBIKE, city="nha_trang", attributes=attrs)


def decide(items: list[RawItem], pp: Passport | None = None, **state: Any) -> Search | Ask:
    return PLANNER.decide(pp or passport(), facets_from(items), DialogueState(**state))


def test_a_small_selection_is_shown_without_questions() -> None:
    assert decide(market(10)) == Search("small")
    assert decide(market(3)) == Search("small")


def test_a_large_selection_gets_the_question_that_cuts_the_most() -> None:
    step = decide(market())
    assert isinstance(step, Ask)
    assert step.question.field in {"attributes.model", "attributes.brand", "budget.max"}
    assert step.question.text.startswith("Подходит 60")


def test_the_question_always_offers_any_and_show_all_with_the_real_count() -> None:
    step = decide(market(45))
    assert isinstance(step, Ask)
    values = [option.value for option in step.question.buttons]
    assert values[-2:] == [SKIP, SHOW_ALL]
    assert step.question.buttons[-1].label == "Показать все 45"
    assert step.question.buttons[-2].label == "Любой"


def test_buttons_carry_counts_and_fit_callback_data() -> None:
    step = decide(market(45))
    assert isinstance(step, Ask)
    for option in step.question.options:
        assert option.label.rsplit(" ", 1)[1].isdigit()
        callback = f"ans:{step.question.code}:{option.value}:{2**40}"
        assert len(callback.encode()) <= 64, callback


def test_a_refusal_to_narrow_stops_the_questions() -> None:
    assert decide(market(), show_all=True) == Search("show_all")


def test_the_question_budget_is_four_and_routing_questions_do_not_count() -> None:
    asked = ("category", "attributes.brand", "attributes.model", "budget.max")
    assert decide(market(), asked=asked) != Search("budget_spent")
    full = ("attributes.brand", "attributes.model", "budget.max", "attributes.rooms")
    assert len(full) == MAX_QUESTIONS
    assert decide(market(), asked=full) == Search("budget_spent")


def test_never_asks_what_is_filled_or_already_asked() -> None:
    items = market()
    seen: set[str] = set()
    pp = passport()
    asked: tuple[str, ...] = ()
    for _ in range(10):
        step = PLANNER.decide(pp, facets_from(items), DialogueState(asked=asked))
        if not isinstance(step, Ask):
            break
        field = step.question.field
        assert field not in seen
        seen.add(field)
        asked = (*asked, field)
    assert len(seen) <= MAX_QUESTIONS
    filled = passport(brand="honda", model="lead", transmission="automatic")
    filled = filled.model_copy(update={"budget": Budget(max=500, currency=Currency.USD)})
    step = PLANNER.decide(filled, facets_from(items), DialogueState())
    assert isinstance(step, Search)


def test_a_field_the_base_barely_knows_is_not_asked() -> None:
    """Известно 35 из 100 (65% пропусков): вопрос резал бы, но базе поле почти неизвестно."""
    brands = [f"brand{n}" for n in range(7)]
    items = [bike_item(i, brand=brands[i % 7]) for i in range(35)] + [
        bike_item(100 + i) for i in range(65)
    ]
    report = facets_from(items)
    facet = report.facets["attributes.brand"]
    assert facet.unknown > 0.6 * report.total
    assert 1 - expected_rest(facet) / report.total >= 0.25
    assert decide(items) == Search("no_useful_question")


def test_a_field_with_a_single_value_never_cuts_even_without_a_gain_floor() -> None:
    items = [bike_item(i, brand="honda") for i in range(40)]
    planner = ClarificationPlanner(min_gain=0.0)
    step = planner.decide(passport(), facets_from(items), DialogueState())
    assert step == Search("no_useful_question")


def test_the_field_with_the_biggest_gain_wins() -> None:
    models = ["lead", "vision", "air_blade", "pcx", "exciter", "attila"]
    items = [
        bike_item(i, brand="honda" if i % 2 else "yamaha", model=models[i % 6]) for i in range(60)
    ]
    step = decide(items)
    assert isinstance(step, Ask)
    assert step.question.field == "attributes.model"


def test_a_question_that_cuts_too_little_is_not_asked() -> None:
    items = [bike_item(i, brand="honda") for i in range(40)] + [bike_item(99, brand="yamaha")]
    assert decide(items) == Search("no_useful_question")


def test_the_choice_is_deterministic() -> None:
    items = market()
    assert decide(items) == decide(list(items))


def test_an_answer_never_enlarges_the_rest() -> None:
    items = market(80)
    report = facets_from(items)
    for name, facet in report.facets.items():
        if facet.known:
            assert expected_rest(facet) <= report.total, name


def test_expected_rest_formula() -> None:
    facet = Facet("attributes.brand", (FacetValue("a", 6), FacetValue("b", 2)), unknown=2)
    assert expected_rest(facet) == pytest.approx(2 + (36 + 4) / 8)
    assert expected_rest(Facet("x", (), unknown=5)) == float("inf")


def test_price_buttons_are_cumulative_and_apply_as_a_vnd_ceiling() -> None:
    items = [bike_item(i, price=(i % 4 + 1) * 5_000_000) for i in range(40)]
    report = facets_from(items)
    step = ClarificationPlanner(min_gain=0.0).decide(
        passport(brand="x", model="y", transmission="automatic"), report, DialogueState()
    )
    assert isinstance(step, Ask)
    assert step.question.field == "budget.max"
    counts = [int(option.label.rsplit(" ", 1)[1]) for option in step.question.options]
    assert counts == sorted(counts)
    assert counts[-1] <= report.total
    assert all(option.value.endswith(" VND") for option in step.question.options)


def test_every_combination_of_filled_fields_is_decided_without_error() -> None:
    items = market(50)
    report = facets_from(items)
    fields = ("brand", "model", "transmission")
    for picked in itertools.chain.from_iterable(
        itertools.combinations(fields, n) for n in range(4)
    ):
        pp = passport(**{key: "x" for key in picked})
        step = PLANNER.decide(pp, report, DialogueState())
        if isinstance(step, Ask):
            assert step.question.field.removeprefix("attributes.") not in picked


def test_an_empty_report_searches() -> None:
    assert PLANNER.decide(passport(), FacetReport(total=0), DialogueState()) == Search("small")


def test_a_value_too_long_for_callback_data_is_not_offered_as_a_button() -> None:
    long_model = "x" * 30  # 30 байт: с кодом поля и корнем ветки не влезет в 64
    items = [bike_item(i, model=long_model if i % 2 else f"m{i % 5}") for i in range(40)]
    step = decide(items)
    assert isinstance(step, Ask)
    values = [option.value for option in step.question.options]
    assert long_model not in values
    assert all(len(v.encode()) <= 24 for v in values)


def test_at_most_four_value_buttons_are_offered() -> None:
    items = [bike_item(i, model=f"m{i % 9}") for i in range(45)]
    step = decide(items)
    assert isinstance(step, Ask)
    assert len(step.question.options) == 4


def test_the_question_says_how_many_do_not_name_the_field() -> None:
    items = [bike_item(i, model=f"m{i % 4}") for i in range(40)] + [
        bike_item(100 + i) for i in range(8)
    ]
    step = decide(items)
    assert isinstance(step, Ask)
    assert step.question.field == "attributes.model"
    assert step.question.text.startswith("Подходит 48, у 8 это не указано. ")
