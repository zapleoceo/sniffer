"""Ответ словами вместо кнопки.

Кнопка — быстрый путь, но половина людей отвечает текстом. Не понять свой же
вопрос — худший способ выглядеть роботом, поэтому разбор проверяется на тех
формулировках, которыми отвечают на самом деле.
"""

from __future__ import annotations

import pytest

from sniffer.domain.dialogue import QUESTIONS, parse_option
from sniffer.domain.passport import Budget, Currency
from sniffer.search.answers import interpret, is_skip


@pytest.mark.parametrize(
    "text",
    ["не важно", "неважно", "да пофиг", "любой", "всё равно", "покажи что есть", "skip"],
)
def test_skip_is_understood_in_words(text: str) -> None:
    assert is_skip(text)


@pytest.mark.parametrize("text", ["до 400", "механика", "honda"])
def test_a_real_answer_is_not_a_skip(text: str) -> None:
    assert not is_skip(text)


def test_budget_in_dollars() -> None:
    value = interpret("budget.max", "до 400")

    assert isinstance(value, Budget)
    assert (value.max, value.currency) == (400, Currency.USD)


def test_budget_in_dong() -> None:
    """Миллионы без валюты — донги: рынок двухвалютный, порог сто тысяч."""
    value = interpret("budget.max", "до 10 млн")

    assert isinstance(value, Budget)
    assert (value.max, value.currency) == (10_000_000, Currency.VND)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("автомат", "automatic"),
        ("только автоматическая", "automatic"),
        ("механика", "manual"),
        ("хочу на механике", "manual"),
        ("полуавтомат", "semi"),
    ],
)
def test_transmission_in_words(text: str, expected: str) -> None:
    """«Полуавтомат» содержит «автомат» — порядок правил тут значим."""
    assert interpret("attributes.transmission", text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("новый", "new"), ("хороший", "good"), ("убитый пойдёт", "worn")],
)
def test_condition_in_words(text: str, expected: str) -> None:
    assert interpret("attributes.condition", text) == expected


@pytest.mark.parametrize(
    ("text", "expected"), [("студия", 1), ("двушку", 2), ("три комнаты", 3), ("2", 2)]
)
def test_rooms_in_words(text: str, expected: int) -> None:
    assert interpret("attributes.rooms", text) == expected


def test_brand_and_category_reuse_the_query_parser() -> None:
    assert interpret("attributes.brand", "хочу yamaha") == "yamaha"
    assert interpret("category", "вообще-то квартиру") == "apartment"


@pytest.mark.parametrize(
    ("field", "label", "value"),
    [
        (question.field, option.label, option.value)
        for question in QUESTIONS
        for option in question.options
    ],
    ids=[
        f"{question.code}-{option.value}" for question in QUESTIONS for option in question.options
    ],
)
def test_every_button_label_is_understood_as_its_own_value(
    field: str, label: str, value: str
) -> None:
    """Ответ словами разбирается тем же знанием, что и кнопка (passport.md).

    Подпись кнопки — самый частый ответ текстом: клиент читает её и печатает.
    Разойдись подпись с разбором, и бот не понял бы собственный вопрос.
    """
    parsed = interpret(field, label)
    expected = parse_option(field, value)

    if isinstance(parsed, Budget):
        # Сравниваются и сумма, И валюта: подпись кнопки называет валюту, и
        # разъехаться значению с подписью нельзя. Раньше сверялась одна сумма,
        # поэтому «до 300 $», уезжавшее как 300 донгов, тест не замечал.
        assert isinstance(expected, Budget)
        assert (parsed.max, parsed.currency) == (expected.max, expected.currency)
    else:
        assert parsed == expected


def test_a_skip_word_inside_a_real_answer_does_not_hide_it() -> None:
    """«любой, лишь бы ездил» — это `worn`, хотя «любой» само по себе пропуск."""
    assert interpret("attributes.condition", "любой, лишь бы ездил") == "worn"
    assert interpret("attributes.condition", "лишь бы на ходу") == "worn"
    assert is_skip("любой"), "без уточнения «любой» остаётся пропуском"


def test_an_answer_that_is_not_an_answer() -> None:
    """На вопрос о бюджете клиент нередко отвечает новым запросом.

    Принять его за сумму значит потерять запрос, поэтому здесь честное `None`.
    """
    assert interpret("budget.max", "ладно, тогда квартиру в Нячанге") is None
    assert interpret("attributes.transmission", "не знаю даже") is None
    assert interpret("districts", "у моря") is None, "поле без вопроса не разбирается"
