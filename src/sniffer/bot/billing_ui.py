"""Кнопки оплаты: экран «Подписка» и ссылка на счёт.

Вся отрисовка кнопок оплаты — здесь, как отрисовка выдачи — в `keyboards.py`. Подписи
берутся из формулировок, а число звёзд — из тарифа: кнопка и счёт не могут разойтись.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sniffer.bot import billing_wording as words

ACCEPT = "accept"
TERMS = "terms"
CANCEL = "cancel"


class BillingCallback(CallbackData, prefix="bill"):
    """Нажатие на экране «Подписка».

    Версия условий едет в кнопке согласия: нажать «принимаю» на экране со старыми
    условиями после их обновления нельзя — согласие записывается ровно с тем текстом,
    который человек видел.
    """

    action: str
    version: str = ""


def confirmation_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=words.ACCEPT_LABEL,
                    callback_data=BillingCallback(
                        action=ACCEPT, version=words.TERMS_VERSION
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=words.TERMS_LABEL, callback_data=BillingCallback(action=TERMS).pack()
                ),
                InlineKeyboardButton(
                    text=words.CANCEL_LABEL, callback_data=BillingCallback(action=CANCEL).pack()
                ),
            ],
        ]
    )


def link_markup(url: str) -> InlineKeyboardMarkup:
    """Ссылка на счёт — кнопкой-адресом: у подписочного счёта кнопки Pay нет."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=words.PAY_LABEL, url=url)]]
    )
