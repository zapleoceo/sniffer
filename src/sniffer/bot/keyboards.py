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
from sniffer.bot.query_menu import title
from sniffer.domain.records import QueryOverview

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
    root: int


class FeedbackCallback(CallbackData, prefix="fb"):
    """Обратная связь на выдаче: «дорого», «не то», «нужен автомат»."""

    kind: str
    root: int


class SubscribeCallback(CallbackData, prefix="sub"):
    """«Следить» привязан к карточкам, под которыми клиент его нажал."""

    root: int


class RequestsCallback(CallbackData, prefix="req"):
    action: str
    root: int = 0


def markup(reply: Reply) -> InlineKeyboardMarkup | None:
    """Разметка сообщения. Нет кнопок — нет и клавиатуры."""
    if reply.question is not None:
        return _rows(
            InlineKeyboardButton(
                text=option.label,
                callback_data=AnswerCallback(
                    code=reply.question.code,
                    value=option.value,
                    root=reply.passport_root or 0,
                ).pack(),
            )
            for option in reply.question.buttons
        )
    if reply.feedback or reply.offer_subscription:
        buttons = [
            InlineKeyboardButton(
                text=option.label,
                callback_data=FeedbackCallback(
                    kind=option.value, root=reply.passport_root or 0
                ).pack(),
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
                        text=SUBSCRIBE_LABEL,
                        callback_data=SubscribeCallback(root=reply.passport_root or 0).pack(),
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="📂 Мои запросы", callback_data=RequestsCallback(action="list").pack()
                )
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)
    return None


def requests_markup(items: list[QueryOverview]) -> InlineKeyboardMarkup:
    icons = {"active": "🟢", "paused": "⏸", "expired": "⌛", "off": "▫️"}
    rows = [
        [
            InlineKeyboardButton(
                text=_request_label(item, icons),
                callback_data=RequestsCallback(action="open", root=item.root).pack(),
            )
        ]
        for item in items
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _request_label(item: QueryOverview, icons: dict[str, str]) -> str:
    selected = "✓ " if item.is_active else ""
    return f"{selected}{icons[item.monitoring]} {title(item.passport)}"


def request_actions(item: QueryOverview) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="🔎 Искать снова",
                callback_data=RequestsCallback(action="search", root=item.root).pack(),
            ),
            InlineKeyboardButton(
                text="✏️ Изменить",
                callback_data=RequestsCallback(action="edit", root=item.root).pack(),
            ),
        ]
    ]
    if item.monitoring == "active":
        rows.append(
            [
                InlineKeyboardButton(
                    text="⏸ Выключить мониторинг",
                    callback_data=RequestsCallback(action="pause", root=item.root).pack(),
                )
            ]
        )
    elif item.monitoring == "paused":
        rows.append(
            [
                InlineKeyboardButton(
                    text="▶️ Включить мониторинг",
                    callback_data=RequestsCallback(action="resume", root=item.root).pack(),
                )
            ]
        )
    elif item.monitoring in {"off", "expired"}:
        rows.append(
            [
                InlineKeyboardButton(
                    text=SUBSCRIBE_LABEL, callback_data=SubscribeCallback(root=item.root).pack()
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="← Все запросы", callback_data=RequestsCallback(action="list").pack()
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _rows(buttons: Iterable[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    flat = list(buttons)
    return InlineKeyboardMarkup(
        inline_keyboard=[flat[start : start + ROW] for start in range(0, len(flat), ROW)]
    )
