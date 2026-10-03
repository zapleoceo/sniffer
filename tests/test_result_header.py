"""Заголовок выдачи называет честный счёт: сколько подходит и сколько показано."""

from __future__ import annotations

from sniffer.bot import wording
from sniffer.domain.passport import Budget, Category, Currency, Passport


def narrow() -> Passport:
    return Passport(
        category=Category.MOTORBIKE,
        budget=Budget(max=500, currency=Currency.USD),
        attributes={"brand": "honda"},
    )


def test_a_narrowed_request_says_how_many_fit_and_how_many_are_shown() -> None:
    assert wording.result_header(narrow(), 36, 10) == "Подходит 36, показываю 10 лучших:"


def test_a_small_selection_is_not_called_a_selection_of_the_best() -> None:
    assert wording.result_header(narrow(), 4, 4) == "Вот что нашлось:"
    assert wording.result_header(narrow(), 1, 1) == "Нашёлся один вариант:"


def test_the_greeting_states_the_free_rule_and_advises_to_narrow() -> None:
    text = wording.GREETING
    assert "Бесплатно — 10 карточек" in text
    assert "лучше сразу сузить" in text
    assert "Скажу, сколько вариантов подходит" in text
