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

from sniffer.bot import wording_plan
from sniffer.bot.billing_wording import SUBSCRIBE_LABEL
from sniffer.bot.conversation import Reply
from sniffer.bot.paging import MoreOffer, all_label, more_label
from sniffer.bot.threads import labels
from sniffer.config import get_settings
from sniffer.domain.records import QueryOverview

# Сколько кнопок в ряд. Три коротких («автомат», «механика», «не важно») в один
# ряд ещё читаются, длинные подписи телефон обрежет.
ROW = 2

# Подпись «Следить» с ценой — `billing_wording.SUBSCRIBE_LABEL`: цена стоит прямо на
# кнопке (кнопка, ведущая к счёту без предупреждения о деньгах, — тёмный паттерн, даже
# если речь про звезду), а число звёзд берётся из тарифа и здесь не пишется.

# Отдельная ветка на каждый поиск — то, из-за чего уточнение не уезжает в чужой
# паспорт. Подпись говорит «новый», а не «сбросить»: прежний поиск остаётся.
NEW_THREAD_LABEL = "➕ Новый поиск"
# Человек видит одно слово — «поиски», — а не «запросы» и «ветки».
SEARCHES_LABEL = "📂 Мои поиски"
ALL_SEARCHES_LABEL = "← Все поиски"


class AnswerCallback(CallbackData, prefix="ans"):
    """Ответ на уточняющий вопрос: код поля и значение кнопки."""

    code: str
    value: str
    root: int


class FeedbackCallback(CallbackData, prefix="fb"):
    """Обратная связь на выдаче: дешевле, «не то», «нужен автомат»."""

    kind: str
    root: int


class SubscribeCallback(CallbackData, prefix="sub"):
    """«Следить» привязан к карточкам, под которыми клиент его нажал."""

    root: int


class RequestsCallback(CallbackData, prefix="req"):
    action: str
    root: int = 0


class PlanCallback(CallbackData, prefix="plan"):
    """Кнопка платного плана под предложением подписки."""

    action: str


class PageCallback(CallbackData, prefix="pg"):
    """«Ещё» / «Показать все»: ключ снимка выдачи, действие и с какого места продолжать."""

    token: str
    action: str
    offset: int


def _page_row(offer: MoreOffer) -> list[InlineKeyboardButton]:
    """Кнопки продолжения; «все» не рисуем, когда оно то же, что и «Ещё»."""
    step = get_settings().max_cards
    row = [
        InlineKeyboardButton(
            text=more_label(min(step, offer.rest)),
            callback_data=PageCallback(
                token=offer.token, action="more", offset=offer.offset
            ).pack(),
        )
    ]
    if offer.rest > step:
        row.append(
            InlineKeyboardButton(
                text=all_label(offer.rest),
                callback_data=PageCallback(
                    token=offer.token, action="all", offset=offer.offset
                ).pack(),
            )
        )
    return row


def without_paging(keyboard: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    """Та же клавиатура без кнопок продолжения: нажатая страница не должна звать второй раз."""
    if keyboard is None:
        return None
    prefix = f"{PageCallback.__prefix__}{PageCallback.__separator__}"
    rows = [
        [button for button in row if not (button.callback_data or "").startswith(prefix)]
        for row in keyboard.inline_keyboard
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


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
    if reply.feedback or reply.offer_subscription or reply.more:
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
        if reply.more is not None:
            # Первой строкой: продолжить выдачу — главное действие под страницей.
            rows.insert(0, _page_row(reply.more))
        if reply.offer_subscription:
            # Отдельной строкой и во всю ширину: это не ещё один вариант
            # обратной связи, а действие с деньгами. Рядом с «дешевле» и «не то»
            # его нажимают, не глядя.
            rows.append(
                [
                    InlineKeyboardButton(
                        text=SUBSCRIBE_LABEL,
                        callback_data=SubscribeCallback(root=reply.passport_root or 0).pack(),
                    )
                ]
            )
        if reply.feedback or reply.offer_subscription:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=SEARCHES_LABEL, callback_data=RequestsCallback(action="list").pack()
                    )
                ]
            )
        return InlineKeyboardMarkup(inline_keyboard=rows)
    if reply.offer_plan:
        # Цена на самой кнопке (R2 §3.5): кнопка, ведущая к деньгам без цифры, — тёмный
        # паттерн. Рядом — выход без оплаты: «ваши поиски» остаются доступны.
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=wording_plan.SUBSCRIBE_LABEL,
                        callback_data=PlanCallback(action="subscribe").pack(),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=SEARCHES_LABEL, callback_data=RequestsCallback(action="list").pack()
                    )
                ],
            ]
        )
    return None


def requests_markup(items: list[QueryOverview], *, marked: bool = True) -> InlineKeyboardMarkup:
    """Список поисков кнопками. `marked=False` — без «✓» (пока взведён `/new`)."""
    icons = {"active": "🟢", "paused": "⏸", "expired": "⌛", "off": "▫️"}
    # Подписи считаются по всему списку сразу: два поиска с одним названием
    # различаются бюджетом, а увидеть это можно только рядом друг с другом.
    names = labels([item.passport for item in items])
    rows = [
        [
            InlineKeyboardButton(
                text=_request_label(item, name, icons, marked=marked),
                callback_data=RequestsCallback(action="open", root=item.root).pack(),
            )
        ]
        for item, name in zip(items, names, strict=True)
    ]
    # Последней строкой, а не первой: человек пришёл сюда за своими поисками, и
    # «новый поиск» над ними превращал бы список в развилку. Кнопка нужна тем,
    # кто про `/new` не знает, — а список и так видно.
    rows.append(
        [
            InlineKeyboardButton(
                text=NEW_THREAD_LABEL, callback_data=RequestsCallback(action="new").pack()
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _request_label(item: QueryOverview, name: str, icons: dict[str, str], *, marked: bool) -> str:
    selected = "✓ " if marked and item.is_active else ""
    return f"{selected}{icons[item.monitoring]} {name}"


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
                text=ALL_SEARCHES_LABEL, callback_data=RequestsCallback(action="list").pack()
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _rows(buttons: Iterable[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    flat = list(buttons)
    return InlineKeyboardMarkup(
        inline_keyboard=[flat[start : start + ROW] for start in range(0, len(flat), ROW)]
    )
