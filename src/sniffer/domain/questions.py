"""Вопрос клиенту и кнопки под ним — данные без логики.

Отдельным модулем, а не частью `dialogue.py`: и реестр полей (`fields.py`), и
планировщик (`clarify.py`) строят вопросы, а `dialogue.py` их реестром
пользуется. Пока Option и Question жили в диалоге, реестру некуда было
импортировать их без цикла.
"""

from __future__ import annotations

from dataclasses import dataclass

from sniffer.domain.passport import Budget

AnswerValue = str | float | Budget

# Значение «не важно». Отдельное от пустого ответа: пустое поле означает «ещё
# не спрашивали», а SKIP — «спросили, клиенту всё равно».
SKIP = "skip"
SKIP_LABEL = "не важно, показать что есть"
# «Показать все»: клиент отказался сужать. Отдельное значение, а не SKIP: «не
# важно» про одно поле, «все» — про весь разговор, и после него вопросов нет.
SHOW_ALL = "all"


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

    `show_all` — сколько всего подходит; не `None` — под вопросом есть кнопка
    «Показать все N». `skip_label` свой у динамических вопросов («Любая»).
    """

    field: str
    code: str
    text: str
    options: tuple[Option, ...] = ()
    skippable: bool = True
    skip_label: str = SKIP_LABEL
    show_all: int | None = None

    @property
    def buttons(self) -> tuple[Option, ...]:
        """Варианты ответа и выходы для полей, которые можно не ограничивать."""
        extra: list[Option] = []
        if self.skippable:
            extra.append(Option(self.skip_label, SKIP))
        if self.show_all is not None:
            extra.append(Option(f"Показать все {self.show_all}", SHOW_ALL))
        return (*self.options, *extra)
