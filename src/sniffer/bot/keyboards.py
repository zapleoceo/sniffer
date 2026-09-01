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

# Цена стоит прямо на кнопке. Кнопка «следить», ведущая к счёту без
# предупреждения о деньгах, — это тёмный паттерн, даже если речь про звезду.
SUBSCRIBE_LABEL = "🔔 Следить за новыми — 1 ⭐/мес"


class AnswerCallback(CallbackData, prefix="ans"):
    """Ответ на уточняющий вопрос: код поля и значение кнопки."""

    code: str
    value: str


class FeedbackCallback(CallbackData, prefix="fb"):
    """Обратная связь на выдаче: «дорого», «не то», «нужен автомат»."""

    kind: str


class SubscribeCallback(CallbackData, prefix="sub"):
    """«Следить за новыми». Данных не несёт: тема берётся из текущего паспорта.

    Класть корень цепочки в `callback_data` было бы удобнее и хуже: кнопка
    живёт в чате неделями, а паспорт за это время сменится, и клиент оплатил бы
    подписку на тот запрос, который видел когда-то, а не на свой нынешний.
    """


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
    if reply.feedback or reply.offer_subscription:
        buttons = [
            InlineKeyboardButton(
                text=option.label,
                callback_data=FeedbackCallback(kind=option.value).pack(),
            )
            for option in reply.feedback
        ]
        rows = [buttons[start : start + ROW] for start in range(0, len(buttons), ROW)]
        if reply.offer_subscription:
            # Отдельной строкой и во всю ширину: это не ещё один вариант
            # обратной связи, а действие с деньгами. Рядом с «дорого» и «не то»
            # его нажимают, не глядя.
            rows.append(
                [
                    InlineKeyboardButton(
                        text=SUBSCRIBE_LABEL, callback_data=SubscribeCallback().pack()
                    )
                ]
            )
        return InlineKeyboardMarkup(inline_keyboard=rows)
    return None


def _rows(buttons: Iterable[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    flat = list(buttons)
    return InlineKeyboardMarkup(
        inline_keyboard=[flat[start : start + ROW] for start in range(0, len(flat), ROW)]
    )
