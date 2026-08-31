"""Правила уточняющего диалога: что спросить, что сделать с ответом.

Домен проверяется отдельно от бота: выбор вопроса — продуктовое правило
(passport.md), и оно обязано быть проверяемым без Telegram, базы и модели.
"""

from __future__ import annotations

import pytest

from sniffer.domain.dialogue import (
    EVENT_MANUAL_EDIT,
    EVENT_QUESTION_ASKED,
    SKIP,
    DialogueState,
    Feedback,
    apply_answer,
    apply_feedback,
    feedback_buttons,
    feedback_question,
    next_question,
    parse_option,
    replay,
)
from sniffer.domain.passport import (
    MAX_CLARIFYING_QUESTIONS,
    Budget,
    Category,
    Currency,
    Intent,
    Passport,
    PassportStatus,
)
from sniffer.domain.records import PassportEvent


def bike(**overrides: object) -> Passport:
    fields: dict[str, object] = {
        "intent": Intent.BUY,
        "category": Category.MOTORBIKE,
        "city": "nha_trang",
        "raw_query": "ищу скутер в Нячанге",
    }
    fields.update(overrides)
    return Passport(**fields)  # type: ignore[arg-type]


def event(kind: str, **payload: object) -> PassportEvent:
    return PassportEvent(passport_id=1, kind=kind, payload=payload)


# ── выбор вопроса ───────────────────────────────────────────────────────────


def test_full_passport_has_nothing_to_ask() -> None:
    """Заполненный паспорт вопросов не порождает: спрашивать нечего, надо искать."""
    filled = bike(
        budget=Budget(max=400, currency=Currency.USD),
        attributes={"transmission": "automatic", "condition": "good", "brand": "honda"},
    )

    assert next_question(filled, asked=()) is None


def test_the_most_informative_field_goes_first() -> None:
    """Бюджет режет выборку вдвое, марка — почти нет (FIELD_INFORMATIVENESS)."""
    question = next_question(bike(), asked=())

    assert question is not None
    assert question.field == "budget.max"


def test_asked_field_is_not_asked_twice() -> None:
    question = next_question(bike(), asked=("budget.max",))

    assert question is not None
    assert question.field == "attributes.transmission"


def test_three_questions_is_the_ceiling() -> None:
    """Дальше показываем выдачу: допрос убивает конверсию сильнее неточной выдачи."""
    asked = ("budget.max", "attributes.transmission", "attributes.condition")

    assert len(asked) == MAX_CLARIFYING_QUESTIONS
    assert next_question(bike(), asked=asked) is None


def test_unknown_category_is_asked_first() -> None:
    question = next_question(Passport(raw_query="что-нибудь"), asked=())

    assert question is not None
    assert question.field == "category"


def test_every_question_can_be_skipped() -> None:
    """Клиент, которого допрашивают без права пропустить, уходит."""
    question = next_question(bike(), asked=())

    assert question is not None
    assert question.buttons[-1].value == SKIP


# ── ответ ───────────────────────────────────────────────────────────────────


def test_button_answer_fills_the_budget() -> None:
    answered = apply_answer(bike(), "budget.max", parse_option("budget.max", "500"))

    assert answered.budget.max == 500
    assert answered.budget.currency is Currency.USD


def test_words_answer_keeps_its_own_currency() -> None:
    """«До 10 млн» — донги, и пересчитывать их в доллары мы не вправе."""
    answered = apply_answer(bike(), "budget.max", Budget(max=10_000_000, currency=Currency.VND))

    assert (answered.budget.max, answered.budget.currency) == (10_000_000, Currency.VND)


def test_answer_does_not_touch_the_previous_passport() -> None:
    """Паспорт неизменяем: правка создаёт новый объект, а не меняет старый."""
    before = bike()

    apply_answer(before, "attributes.transmission", "automatic")

    assert before.attributes == {}


def test_answer_updates_what_is_still_missing() -> None:
    answered = apply_answer(bike(), "budget.max", 400)

    assert "budget.max" not in answered.missing_fields
    assert answered.status is PassportStatus.READY


def test_category_answer_switches_the_question_set() -> None:
    answered = apply_answer(Passport(raw_query="что-нибудь"), "category", "apartment")
    question = next_question(answered, asked=("budget.max",))

    assert answered.category is Category.APARTMENT
    assert question is not None
    assert question.field == "attributes.rooms"


def test_unknown_field_is_a_bug_not_a_silent_no_op() -> None:
    with pytest.raises(ValueError, match="не заполняется"):
        apply_answer(bike(), "confidence", "0.9")


# ── обратная связь ──────────────────────────────────────────────────────────


def test_pricey_cuts_the_budget() -> None:
    cheaper = apply_feedback(bike(budget=Budget(max=500, currency=Currency.USD)), Feedback.PRICEY)

    assert cheaper is not None
    assert cheaper.budget.max == 350


def test_pricey_without_a_budget_asks_instead_of_guessing() -> None:
    """Сколько «дорого» в цифрах, мы не знаем: бюджет не назывался."""
    passport = bike()

    assert apply_feedback(passport, Feedback.PRICEY) is None
    question = feedback_question(passport, Feedback.PRICEY, asked=())
    assert question is not None
    assert question.field == "budget.max"


def test_automatic_feedback_fills_transmission() -> None:
    fixed = apply_feedback(bike(), Feedback.AUTOMATIC)

    assert fixed is not None
    assert fixed.attributes["transmission"] == "automatic"


def test_wrong_feedback_asks_one_more_question_over_the_limit() -> None:
    """Три вопроса — защита от допроса ДО выдачи; здесь клиент сам нажал кнопку."""
    asked = ("budget.max", "attributes.transmission", "attributes.condition")

    assert next_question(bike(), asked=asked) is None
    question = feedback_question(bike(), Feedback.WRONG, asked=asked)
    assert question is not None
    assert question.field == "attributes.brand"


def test_automatic_button_is_offered_only_where_it_means_something() -> None:
    values = {option.value for option in feedback_buttons(bike())}
    apartment = {option.value for option in feedback_buttons(bike(category=Category.APARTMENT))}

    assert Feedback.AUTOMATIC.value in values
    assert Feedback.AUTOMATIC.value not in apartment
    assert {Feedback.PRICEY.value, Feedback.WRONG.value} <= apartment


# ── состояние диалога ───────────────────────────────────────────────────────


def test_state_is_replayed_from_the_event_log() -> None:
    state = replay(
        [
            event("user_message", text="ищу скутер"),
            event(EVENT_QUESTION_ASKED, field="budget.max"),
            event(EVENT_MANUAL_EDIT, field="budget.max", value="500"),
            event(EVENT_QUESTION_ASKED, field="attributes.transmission"),
        ]
    )

    assert state == DialogueState(
        asked=("budget.max", "attributes.transmission"), pending="attributes.transmission"
    )


def test_answer_closes_the_pending_question() -> None:
    state = replay(
        [
            event(EVENT_QUESTION_ASKED, field="budget.max"),
            event(EVENT_MANUAL_EDIT, field="budget.max", skipped=True),
        ]
    )

    assert state.pending is None
    assert state.asked == ("budget.max",), "пропуск не отменяет того, что вопрос был задан"
