"""Уточняющий диалог: какой вопрос задать и что делать с ответом.

Правило продукта (passport.md): спрашиваем только поля, которые реально сужают
выдачу, и не больше трёх вопросов за диалог. Дальше показываем выдачу и
уточняем паспорт обратной связью на карточках — человеку проще сказать
«дорого», глядя на пять карточек, чем назвать бюджет в пустоту.

Модуль чистый: ни ввода-вывода, ни aiogram, ни знания о Telegram. Бот только
показывает выбранный здесь вопрос и приносит обратно ответ.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from sniffer.domain.passport import (
    FIELD_INFORMATIVENESS,
    MAX_CLARIFYING_QUESTIONS,
    Budget,
    Category,
    Currency,
    Passport,
    PassportStatus,
    has_value,
    next_questions,
)
from sniffer.domain.records import PassportEvent

# Значение «не важно». Отдельное от пустого ответа: пустое поле означает «ещё
# не спрашивали», а SKIP — «спросили, клиенту всё равно».
SKIP = "skip"
SKIP_LABEL = "не важно, показать что есть"

# Виды событий паспорта (passport.md, «Версионирование»). `question_asked` —
# не правка паспорта, а след диалога: из него собирается счётчик заданных
# вопросов, который обязан пережить перезапуск процесса.
EVENT_USER_MESSAGE = "user_message"
EVENT_FEEDBACK = "feedback"
EVENT_AGENT_INFER = "agent_infer"
EVENT_MANUAL_EDIT = "manual_edit"
EVENT_QUESTION_ASKED = "question_asked"

# Насколько режем бюджет по кнопке «дорого». Не в ноль и не в половину:
# клиент отбраковал показанное, а не отказался от покупки.
PRICEY_FACTOR = 0.7

AnswerValue = str | float | Budget


class Feedback(StrEnum):
    PRICEY = "pricey"
    WRONG = "wrong"
    AUTOMATIC = "automatic"


@dataclass(frozen=True, slots=True)
class Option:
    """Кнопка ответа. `value` уезжает в callback_data, поэтому короткий."""

    label: str
    value: str


@dataclass(frozen=True, slots=True)
class Question:
    """Вопрос про одно поле паспорта.

    `code` — короткий ключ поля для callback_data: в неё влезает 64 байта, а
    `attributes.transmission` съело бы треть бюджета кириллицей.
    """

    field: str
    code: str
    text: str
    options: tuple[Option, ...] = ()

    @property
    def buttons(self) -> tuple[Option, ...]:
        """Кнопки вопроса. «Не важно» здесь, а не в каталоге, — чтобы её было
        нельзя забыть: клиент, которого допрашивают без права пропустить, уходит.
        """
        return (*self.options, Option(SKIP_LABEL, SKIP))


# Спрашиваем только про то, что умеем спросить кнопками. Поле без вопроса
# (например `districts` — справочника районов пока нет) просто пропускается:
# лучше не спросить, чем спросить так, что ответ нечем разобрать.
QUESTIONS: tuple[Question, ...] = (
    Question(
        field="category",
        code="cat",
        text="Что ищем?",
        options=(
            Option("скутер", "motorbike"),
            Option("квартиру", "apartment"),
            Option("комнату", "room"),
            Option("дом", "house"),
        ),
    ),
    Question(
        field="budget.max",
        code="budget",
        text="Какой бюджет? Можно написать словами — «до 400» или «до 10 млн».",
        options=(
            Option("до 300 $", "300"),
            Option("до 500 $", "500"),
            Option("до 800 $", "800"),
        ),
    ),
    Question(
        field="attributes.transmission",
        code="trans",
        text="Автомат или механика?",
        options=(Option("автомат", "automatic"), Option("механика", "manual")),
    ),
    Question(
        field="attributes.condition",
        code="cond",
        text="Состояние?",
        options=(
            Option("новый", "new"),
            Option("хороший", "good"),
            Option("любой, лишь бы ездил", "worn"),
        ),
    ),
    Question(
        field="attributes.brand",
        code="brand",
        text="Есть марка на примете?",
        options=(Option("Honda", "honda"), Option("Yamaha", "yamaha")),
    ),
    Question(
        field="attributes.rooms",
        code="rooms",
        text="Сколько комнат?",
        options=(Option("студия", "1"), Option("две", "2"), Option("три и больше", "3")),
    ),
)

_BY_FIELD: dict[str, Question] = {question.field: question for question in QUESTIONS}
_BY_CODE: dict[str, Question] = {question.code: question for question in QUESTIONS}


def question_for(field: str) -> Question | None:
    return _BY_FIELD.get(field)


def question_by_code(code: str) -> Question | None:
    return _BY_CODE.get(code)


def next_question(
    passport: Passport,
    asked: Sequence[str],
    *,
    limit: int = MAX_CLARIFYING_QUESTIONS,
) -> Question | None:
    """Следующий вопрос — или ничего, если пора показывать выдачу.

    `limit` считает вопросы всего диалога, а не оставшиеся: цепочка версий
    паспорта помнит, о чём уже спрашивали, и после перезапуска бот не начинает
    допрос заново.
    """
    if len(asked) >= limit:
        return None
    for field in _ranked(passport):
        if field in asked:
            continue
        question = question_for(field)
        if question is not None:
            return question
    return None


def _ranked(passport: Passport) -> list[str]:
    """Незаполненные поля по убыванию информативности — весь список, не топ-3.

    Отсев уже спрошенных и неспрашиваемых полей идёт после ранжирования:
    обрезать список до лимита раньше отсева значило бы промолчать там, где
    следующее поле спросить было можно.
    """
    if passport.category is None:
        # Без категории спрашивать больше нечего: набор полей зависит от неё.
        return next_questions(passport, limit=1)
    weights = FIELD_INFORMATIVENESS.get(passport.category, {})
    return next_questions(passport, limit=len(weights) or 1)


def parse_option(field: str, raw: str) -> AnswerValue:
    """Значение кнопки → значение поля паспорта."""
    if field == "budget.max":
        return float(raw)
    if field == "attributes.rooms":
        return int(raw)
    return raw


def apply_answer(passport: Passport, field: str, value: AnswerValue) -> Passport:
    """Ответ клиента → новая версия паспорта.

    Паспорт неизменяем (passport.md): здесь появляется новый объект, а версию
    ему присваивает репозиторий. Пропуск («не важно») сюда не доходит вовсе —
    он ничего не меняет и остаётся только событием.
    """
    update: dict[str, object] = {}
    if field == "budget.max":
        update["budget"] = _budget(passport, value)
    elif field == "category":
        update["category"] = Category(str(value))
    elif field == "districts":
        update["districts"] = [str(value)]
    elif field.startswith("attributes."):
        update["attributes"] = {**passport.attributes, field.removeprefix("attributes."): value}
    else:  # pragma: no cover — поля вне каталога вопросов сюда не приходят
        raise ValueError(f"поле {field!r} не заполняется ответом клиента")

    revised = passport.model_copy(update=update)
    return revised.model_copy(
        update={
            "missing_fields": [
                name for name in ("category", "city", "budget.max") if not has_value(revised, name)
            ],
            "status": PassportStatus.READY if revised.is_ready() else revised.status,
        }
    )


def _budget(passport: Passport, value: AnswerValue) -> Budget:
    """Кнопки называют доллары, слова могут назвать что угодно.

    Период не трогаем: его выбрал разбор запроса по намерению (аренда —
    помесячно, покупка — разово), и ответ про сумму об этом ничего не говорит.
    """
    if isinstance(value, Budget):
        return Budget(
            min=passport.budget.min,
            max=value.max,
            currency=value.currency or passport.budget.currency,
            period=passport.budget.period,
        )
    return Budget(
        min=passport.budget.min,
        max=float(value),
        currency=passport.budget.currency or Currency.USD,
        period=passport.budget.period,
    )


def feedback_buttons(passport: Passport) -> tuple[Option, ...]:
    """Кнопки под выдачей. Зависят от паспорта: «нужен автомат» под квартирой — мусор."""
    buttons = [Option("дорого", Feedback.PRICEY.value), Option("не то", Feedback.WRONG.value)]
    if passport.category is Category.MOTORBIKE and not passport.attributes.get("transmission"):
        buttons.append(Option("нужен автомат", Feedback.AUTOMATIC.value))
    return tuple(buttons)


def apply_feedback(passport: Passport, kind: Feedback) -> Passport | None:
    """Обратная связь → новая версия паспорта. `None` — менять нечего, надо спросить."""
    if kind is Feedback.PRICEY:
        if not passport.budget.max:
            # Сколько «дорого» в цифрах, мы не знаем: бюджет не назывался.
            return None
        return apply_answer(passport, "budget.max", round(passport.budget.max * PRICEY_FACTOR))
    if kind is Feedback.AUTOMATIC:
        return apply_answer(passport, "attributes.transmission", "automatic")
    # «Не то» само по себе ничего не уточняет — это просьба спросить ещё раз.
    return None


def feedback_question(passport: Passport, kind: Feedback, asked: Sequence[str]) -> Question | None:
    """Что спросить, когда обратную связь не во что превратить.

    Лимит поднимается на один вопрос выше обычного: три вопроса — это защита от
    допроса до выдачи, а здесь клиент сам нажал кнопку и ждёт уточнения.
    """
    if kind is Feedback.PRICEY:
        return question_for("budget.max")
    return next_question(passport, asked, limit=len(asked) + 1)


@dataclass(frozen=True, slots=True)
class DialogueState:
    """Сколько уже спросили и ждём ли ответ. Собирается из `passport_events`."""

    asked: tuple[str, ...] = ()
    pending: str | None = None


def advance(state: DialogueState, kind: str, payload: dict[str, object]) -> DialogueState:
    """Одно событие двигает состояние диалога."""
    if kind != EVENT_QUESTION_ASKED:
        # Любое другое событие — это реакция клиента: вопрос закрыт.
        return DialogueState(asked=state.asked, pending=None)
    field = str(payload.get("field") or "")
    if not field:  # pragma: no cover — событие без поля мы не пишем
        return state
    asked = state.asked if field in state.asked else (*state.asked, field)
    return DialogueState(asked=asked, pending=field)


def replay(events: Sequence[PassportEvent]) -> DialogueState:
    """Лог событий цепочки → состояние диалога.

    Состояние не хранится отдельной колонкой намеренно: оно выводится из
    истории, которую паспорт обязан вести и без диалога, и поэтому не может с
    ней разъехаться.
    """
    state = DialogueState()
    for event in events:
        state = advance(state, event.kind, dict(event.payload))
    return state
