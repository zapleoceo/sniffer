"""Панель «Мои слежения»: слоты, статусы поисков, пауза, перенос слота, «Удалить поиск».

Только отрисовка и провод кнопок (`wch:<действие>:<корень>:<цель>`); решения принимает
`watch_flow`, база — `db/repositories/watch.py`. Названия поисков идут через `labels`:
два поиска с одним названием различаются бюджетом, и увидеть это можно только рядом.

Слот — оплаченная подписка (`plans.SUBSCRIPTION_STARS` ⭐/мес). Панель говорит ровно то,
что есть: сколько куплено, сколько привязано к поискам, сколько поисков из предела. «Удалить
поиск» не удаляет версии и не возвращает деньги: слежение встаёт на паузу, а оплаченный слот
остаётся за клиентом до конца срока и его можно перенести на другой поиск.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sniffer.bot.keyboards import SubscribeCallback
from sniffer.bot.naming import plural
from sniffer.bot.slot_wording import FOLLOW_LABEL
from sniffer.bot.threads import labels
from sniffer.bot.watch_button import LIST, WatchCallback
from sniffer.domain import plans
from sniffer.domain.records import QueryOverview

CARD, PAUSE, RESUME = "c", "p", "r"
MOVE, MOVE_TO, DELETE, DELETE_OK, NEW, TAB = "m", "t", "d", "k", "n", "w"

PAID_SEARCHES = plans.PAID_SEARCHES
ICONS = {"active": "🟢", "paused": "⏸", "expired": "⌛", "off": "▫️"}
BACK_LABEL = "← Мои слежения"
DELETE_LABEL = "🗑 Удалить поиск"
EMPTY = "Поисков пока нет. Напишите, что хотите найти."
NO_FREE_TARGET = "Переносить некуда: у всех ваших поисков слежение уже включено."
LIMIT_REACHED = "У вас уже {used} {noun} — поставьте на паузу или удалите один{upgrade}."
UPGRADE = ", либо оформите подписку: тогда можно держать до {paid}"
_NOUN = ("поиск", "поиска", "поисков")
MOVED = "Слот перенесён: слежение идёт за новым поиском, с этого момента."
MOVE_REFUSED = "Не получилось перенести слот: он уже изменился. Откройте панель заново."
TAB_OPENED = "Открыл вкладку «{name}»: пишите про этот поиск там."
TAB_FAILED = "Не получилось открыть вкладку. Поиск работает и здесь, в чате."
NEW_TAB = "Открыл вкладку «{name}». Напишите в ней, что ищете."
REPLACED = "Прежний поиск убран, слежение за ним остановлено; версии сохранены."
KEPT = "Оставил текущий поиск."
DELETED = "Поиск убран. Слежение за ним остановлено; версии сохранены."


@dataclass(frozen=True, slots=True)
class PanelView:
    items: list[QueryOverview]
    paid_slots: int  # оплаченных подписок
    bound_slots: int  # из них привязано к поискам
    used: int  # поисков в работе
    cap: int  # предел поисков для этого аккаунта


def limit_text(view: PanelView) -> str:
    """Отказ открыть ещё один поиск: сколько их уже и что с этим делать."""
    upgrade = "" if view.paid_slots else UPGRADE.format(paid=PAID_SEARCHES)
    return LIMIT_REACHED.format(used=view.used, noun=plural(view.used, _NOUN), upgrade=upgrade)


def panel_text(view: PanelView) -> str:
    head = [
        "<b>🔔 Мои слежения</b>",
        f"Слоты: куплено {view.paid_slots}, занято {view.bound_slots}",
        f"Поисков: {view.used} из {view.cap}",
    ]
    if not view.items:
        return "\n".join([*head, "", EMPTY])
    names = labels([item.passport for item in view.items])
    rows = [
        f"{ICONS[item.monitoring]} {escape(name, quote=False)}"
        for item, name in zip(view.items, names, strict=True)
    ]
    return "\n".join([*head, "", *rows])


def panel_markup(view: PanelView) -> InlineKeyboardMarkup:
    names = labels([item.passport for item in view.items])
    rows = [
        [
            InlineKeyboardButton(
                text=f"{ICONS[item.monitoring]} {name}",
                callback_data=WatchCallback(a=CARD, root=item.root).pack(),
            )
        ]
        for item, name in zip(view.items, names, strict=True)
    ]
    rows.append(
        [InlineKeyboardButton(text="➕ Новый поиск", callback_data=WatchCallback(a=NEW).pack())]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def card_footer(
    item: QueryOverview, *, offer_tab: bool = False
) -> list[list[InlineKeyboardButton]]:
    """Ряды управления слежением под карточкой фильтра.

    `offer_tab` — режим тем включён, а темы у поиска нет (или она утрачена).
    """
    root = item.root
    rows: list[list[InlineKeyboardButton]] = []
    if offer_tab:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🗂 Вкладка", callback_data=WatchCallback(a=TAB, root=root).pack()
                )
            ]
        )
    if item.monitoring in {"active", "paused"}:
        toggle = (PAUSE, "⏸ Пауза") if item.monitoring == "active" else (RESUME, "▶️ Включить")
        rows.append(
            [
                InlineKeyboardButton(
                    text=toggle[1], callback_data=WatchCallback(a=toggle[0], root=root).pack()
                ),
                InlineKeyboardButton(
                    text="🔀 Перенести слот",
                    callback_data=WatchCallback(a=MOVE, root=root).pack(),
                ),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text=FOLLOW_LABEL, callback_data=SubscribeCallback(root=root).pack()
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=DELETE_LABEL, callback_data=WatchCallback(a=DELETE, root=root).pack()
            ),
            InlineKeyboardButton(text=BACK_LABEL, callback_data=WatchCallback(a=LIST).pack()),
        ]
    )
    return rows


def move_markup(source: QueryOverview, others: list[QueryOverview]) -> InlineKeyboardMarkup:
    """Куда перенести слот: поиски без живого слежения."""
    targets = [o for o in others if o.root != source.root and o.monitoring in {"off", "expired"}]
    names = labels([o.passport for o in targets])
    rows = [
        [
            InlineKeyboardButton(
                text=name,
                callback_data=WatchCallback(a=MOVE_TO, root=source.root, to=o.root).pack(),
            )
        ]
        for o, name in zip(targets, names, strict=True)
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="← Назад", callback_data=WatchCallback(a=CARD, root=source.root).pack()
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def delete_text(item: QueryOverview) -> str:
    slot = ""
    if item.monitoring in {"active", "paused"}:
        slot = (
            " Оплаченный слот останется за вами до конца срока — его можно "
            "перенести на другой поиск."
        )
    return f"Убрать этот поиск из списка и остановить слежение?{slot} Это не отменить кнопкой."


def delete_markup(root: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, убрать",
                    callback_data=WatchCallback(a=DELETE_OK, root=root).pack(),
                    style="danger",
                ),
                InlineKeyboardButton(
                    text="Отмена", callback_data=WatchCallback(a=CARD, root=root).pack()
                ),
            ]
        ]
    )
