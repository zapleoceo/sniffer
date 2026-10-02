"""Тексты бота как чистые функции от паспорта: без диалога, базы и Telegram.

Пока формулировки жили в `conversation.py`, их проверял только целый диалог, и
два пути не ходил никто: «категории нет» и «категория без своего совета».
Такой запрос приходит (`Category.OTHER`, пустой паспорт после сбоя разбора), и
ему обязан достаться общий текст, а не исключение.
"""

from __future__ import annotations

import pytest

from sniffer.bot import wording
from sniffer.domain.passport import Category, Intent, Passport

KNOWN = [category for category in Category if category is not Category.OTHER]
NO_SUBJECT = [None, Category.OTHER]


@pytest.mark.parametrize("category", NO_SUBJECT)
def test_a_broad_query_without_a_known_subject_gets_the_general_advice(
    category: Category | None,
) -> None:
    header = wording.result_header(Passport(category=category), total=40, shown=5)

    assert header.splitlines() == [
        "Запрос широкий — нашлось много (40). Показываю 5 самых свежих.",
        "Чтобы сузить, допишите бюджет, район или обязательные условия.",
    ]


@pytest.mark.parametrize("category", KNOWN)
def test_every_real_category_is_named_in_the_broad_header(category: Category) -> None:
    """Новая категория без названия во множественном числе краснит тест, а не молчит."""
    header = wording.result_header(Passport(category=category), total=40, shown=5)

    assert not header.startswith("Запрос широкий — нашлось много"), category


@pytest.mark.parametrize("category", NO_SUBJECT)
def test_an_empty_result_for_an_unknown_subject_loosens_the_general_criteria(
    category: Category | None,
) -> None:
    assert wording.nothing_found(Passport(category=category)) == (
        "По этому запросу ничего не нашлось. Попробуйте изменить бюджет, место "
        "или обязательные условия."
    )


@pytest.mark.parametrize("category", [Category.APARTMENT, Category.ROOM, Category.HOUSE])
def test_an_empty_housing_result_suggests_housing_criteria(category: Category) -> None:
    text = wording.nothing_found(Passport(category=category))

    assert "район" in text
    assert "марк" not in text


@pytest.mark.parametrize("category", [Category.MOTORBIKE, Category.CAR, Category.BICYCLE])
def test_an_empty_vehicle_result_suggests_dropping_the_brand(category: Category) -> None:
    assert wording.nothing_found(Passport(category=category)) == wording.NOTHING_FOUND


def test_the_acceptance_line_names_what_was_understood_even_without_a_subject() -> None:
    assert wording.accepted(Passport()) == (
        "Понял: запрос как есть. Ищу подходящие предложения, это занимает до минуты."
    )


@pytest.mark.parametrize(
    ("intent", "action"),
    [
        (Intent.RENT, "Ищу варианты аренды"),
        (Intent.SELL, "Ищу покупателей"),
        (Intent.RENT_OUT, "Ищу арендаторов"),
    ],
)
def test_the_acceptance_line_follows_the_intent(intent: Intent, action: str) -> None:
    assert wording.accepted(Passport(intent=intent)).endswith(f"{action}, это занимает до минуты.")


def test_a_scooter_is_called_a_scooter_not_a_motorbike() -> None:
    passport = Passport(category=Category.MOTORBIKE, attributes={"body_type": "tay_ga"})

    assert wording.accepted(passport).startswith("Понял: скутер.")
