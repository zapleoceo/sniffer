"""Данные кнопки «Мои слежения»: callback и разметка отказа по пределу поисков.

Лист без зависимостей от `keyboards` и `watch_panel`: оба нуждаются в этой кнопке
(`keyboards` — отказ обычным текстом, `watch_panel` — отказ на `/new` и всё остальное),
а `watch_panel` сам импортирует `keyboards`. Данные кнопки в общем модуле рвут цикл,
а не прячут его отложенным импортом.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

LIST, REPLACE, KEEP = "l", "x", "o"
PANEL_LABEL = "🔔 Мои слежения"
REPLACE_LABEL = "🔁 Заменить"
KEEP_LABEL = "Оставить"


class WatchCallback(CallbackData, prefix="wch"):
    a: str
    root: int = 0
    to: int = 0


def limit_markup() -> InlineKeyboardMarkup:
    """Кнопка к панели: там пауза и «Удалить поиск» у каждого поиска."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=PANEL_LABEL, callback_data=WatchCallback(a=LIST).pack())]
        ]
    )


def replace_markup(root: int) -> InlineKeyboardMarkup:
    """Отказ `/new` у бесплатного с одним поиском: заменить его новым или оставить.

    Первой строкой — та же кнопка панели, что и у любого отказа: человек, который хочет
    разобраться сам, идёт туда, а «Заменить» и «Оставить» — быстрый путь для одного поиска.
    """
    rows = limit_markup().inline_keyboard
    rows.append(
        [
            InlineKeyboardButton(
                text=REPLACE_LABEL, callback_data=WatchCallback(a=REPLACE, root=root).pack()
            ),
            InlineKeyboardButton(
                text=KEEP_LABEL, callback_data=WatchCallback(a=KEEP, root=root).pack()
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
