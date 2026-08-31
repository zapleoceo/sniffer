"""Кнопки под вопросом и под выдачей.

Вся отрисовка ответа доменной модели — здесь, и только здесь. Домен решает,
что спросить и что предложить нажать; этот файл превращает решение в разметку
Telegram и обратно.

В `callback_data` влезает 64 байта, поэтому по проводу едут короткие ключи
(`trans`, `automatic`), а не имена полей паспорта: `attributes.transmission`
съело бы треть бюджета, а кириллическая подпись — весь.
"""

from __future__ import annotations

from collections.abc import Iterable

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sniffer.bot.conversation import Reply

# Сколько кнопок в ряд. Три коротких («автомат», «механика», «не важно») в один
# ряд ещё читаются, длинные подписи телефон обрежет.
ROW = 2


class AnswerCallback(CallbackData, prefix="ans"):
    """Ответ на уточняющий вопрос: код поля и значение кнопки."""

    code: str
    value: str


class FeedbackCallback(CallbackData, prefix="fb"):
    """Обратная связь на выдаче: «дорого», «не то», «нужен автомат»."""

    kind: str


def markup(reply: Reply) -> InlineKeyboardMarkup | None:
    """Разметка сообщения. Нет кнопок — нет и клавиатуры."""
    if reply.question is not None:
        return _rows(
            InlineKeyboardButton(
                text=option.label,
                callback_data=AnswerCallback(code=reply.question.code, value=option.value).pack(),
            )
            for option in reply.question.buttons
        )
    if reply.feedback:
        return _rows(
            InlineKeyboardButton(
                text=option.label,
                callback_data=FeedbackCallback(kind=option.value).pack(),
            )
            for option in reply.feedback
        )
    return None


def _rows(buttons: Iterable[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    flat = list(buttons)
    return InlineKeyboardMarkup(
        inline_keyboard=[flat[start : start + ROW] for start in range(0, len(flat), ROW)]
    )
