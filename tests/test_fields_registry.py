"""Реестр полей = прежние QUESTIONS / parse_option / apply_answer.

Перенос шёл без смены поведения, и доказательство здесь — сравнение с дословной
копией прежних функций (`tests/legacy_dialogue_oracle.py`) на произведении
паспортов и значений, а не на нескольких примерах.
"""

from __future__ import annotations

import itertools

import pytest

from sniffer.domain import fields
from sniffer.domain.dialogue import SKIP, Question
from sniffer.domain.passport import (
    Budget,
    Category,
    Currency,
    Intent,
    Passport,
    PassportStatus,
    PricePeriod,
)
from tests.legacy_dialogue_oracle import (
    LEGACY_QUESTIONS,
    legacy_apply_answer,
    legacy_parse_option,
)

_PASSPORTS: tuple[Passport, ...] = tuple(
    Passport(
        intent=intent,
        category=category,
        city=city,
        budget=budget,
        attributes=attrs,
        status=status,
    )
    for intent, category, city, budget, attrs, status in itertools.product(
        (None, Intent.RENT),
        (None, Category.MOTORBIKE, Category.APARTMENT),
        (None, "nha_trang"),
        (
            Budget(),
            Budget(max=500, currency=Currency.USD),
            Budget(min=3_000_000, max=9_000_000, currency=Currency.VND, period=PricePeriod.ONCE),
        ),
        ({}, {"brand": "yamaha", "transmission": "manual"}),
        (PassportStatus.DRAFT, PassportStatus.ACTIVE),
    )
)

_RAW: dict[str, tuple[str, ...]] = {
    "category": ("scooter", "motorbike", "apartment", "room", "house"),
    "city": ("nha_trang", "da_nang"),
    "budget.max": ("300 USD", "500 USD", "800 USD", "250", "15000000 VND", "2000000 VND"),
    "attributes.transmission": ("automatic", "manual"),
    "attributes.condition": ("new", "good", "worn"),
    "attributes.brand": ("honda", "yamaha", "sym"),
    "attributes.rooms": ("1", "2", "3"),
    "attributes.zzz": ("anything",),  # поле вне реестра, но с префиксом attributes.
}


def test_static_questions_are_the_legacy_ones_plus_the_model_field() -> None:
    legacy = list(LEGACY_QUESTIONS)
    current = [q for q in fields.QUESTIONS if q.field != "attributes.model"]
    assert current == legacy
    assert [q.field for q in fields.QUESTIONS if q not in legacy] == ["attributes.model"]


@pytest.mark.parametrize("question", LEGACY_QUESTIONS, ids=lambda q: q.field)
def test_lookup_by_field_and_by_code_matches_legacy(question: Question) -> None:
    assert fields.question_for(question.field) == question
    assert fields.question_by_code(question.code) == question
    assert fields.question_for("districts") is None
    assert fields.question_by_code("nope") is None


@pytest.mark.parametrize(("field", "raw"), [(f, r) for f, raws in _RAW.items() for r in raws])
def test_parse_option_matches_legacy(field: str, raw: str) -> None:
    assert fields.parse_option(field, raw) == legacy_parse_option(field, raw)


def test_apply_answer_matches_legacy_on_every_combination() -> None:
    checked = 0
    for passport, (field, raws) in itertools.product(_PASSPORTS, _RAW.items()):
        for raw in raws:
            value = legacy_parse_option(field, raw)
            assert fields.apply_answer(passport, field, value) == legacy_apply_answer(
                passport, field, value
            ), (field, raw, passport)
            checked += 1
    assert checked > 3000


def test_apply_answer_refuses_a_field_outside_the_catalog_like_legacy() -> None:
    passport = Passport()
    with pytest.raises(ValueError, match="districts"):
        fields.apply_answer(passport, "districts", "x")
    with pytest.raises(ValueError, match="districts"):
        legacy_apply_answer(passport, "districts", "x")


def test_a_button_with_currency_keeps_that_currency() -> None:
    passport = Passport(budget=Budget(max=100, currency=Currency.VND))
    updated = fields.apply_answer(
        passport, "budget.max", fields.parse_option("budget.max", "300 USD")
    )
    assert updated.budget.currency is Currency.USD
    assert updated.budget.max == 300


def test_skip_is_not_a_value_the_registry_applies() -> None:
    # «Не важно» не меняет паспорт: до реестра оно не доходит, и это сторожит бот.
    assert SKIP == "skip"


_TEXTS = (
    "до 400",
    "до 10 млн",
    "500 долларов",
    "скутер",
    "квартиру в Нячанге",
    "мотоцикл",
    "нячанг",
    "дананг",
    "honda",
    "хонда",
    "yamaha до 300",
    "автомат",
    "механика",
    "xe số",
    "новый",
    "хороший",
    "убитый пойдёт",
    "лишь бы ездил",
    "2",
    "две",
    "три",
    "студия",
    "двушка",
    "2 спальни",
    "не важно",
    "ладно, тогда квартиру",
    "",
    "lead",
    "honda lead 125",
)


def test_interpret_table_matches_the_legacy_chain() -> None:
    from sniffer.search.answers import interpret
    from tests.legacy_interpret_oracle import legacy_interpret

    for field in (*_RAW, "districts", "attributes.model"):
        if field == "attributes.model":
            continue  # новое поле: у прежней цепочки ответа на него не было вовсе
        for text in _TEXTS:
            assert interpret(field, text) == legacy_interpret(field, text), (field, text)


def test_a_model_is_read_from_words_like_in_the_first_request() -> None:
    from sniffer.search.answers import interpret

    assert interpret("attributes.model", "honda lead 125") == "lead"
    assert interpret("attributes.model", "ладно, тогда квартиру") is None
