"""Вызовы Bot API для оплаты: единственное место, где сервис оплаты знает aiogram.

Адаптер протокола `BotApi` (`billing_ports.py`). Ошибки Telegram превращаются в
`BotApiError` с описанием без токена: в адресе запроса, который иногда попадает в текст
сетевой ошибки, токен бота есть, а текст уходит владельцу в чат и в лог.
"""

from __future__ import annotations

import re

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import LabeledPrice

from sniffer.bot.billing_ports import BotApiError
from sniffer.domain import plans

# `bot<id>:<секрет>` из адреса запроса.
_TOKEN = re.compile(r"bot[0-9]+:[A-Za-z0-9_-]+")


def redact(text: str) -> str:
    return _TOKEN.sub("bot***", text)


class AiogramBotApi:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def create_invoice_link(
        self, *, title: str, description: str, payload: str, label: str, amount: int, period_s: int
    ) -> str:
        """Подписочный счёт — только ссылкой: `subscription_period` есть у этого метода.

        У `sendInvoice` такого параметра нет, и aiogram молча пропускал его как лишнее
        поле запроса: по документации Telegram клиент покупал разовый платёж либо не мог
        купить вовсе. Валюта — звёзды, `provider_token` не нужен (пустой он на провод не
        попадает).
        """
        try:
            return await self._bot.create_invoice_link(
                title=title,
                description=description,
                payload=payload,
                currency=plans.SUBSCRIPTION_CURRENCY,
                prices=[LabeledPrice(label=label, amount=amount)],
                subscription_period=period_s,
            )
        except TelegramAPIError as exc:
            raise BotApiError(redact(exc.message)) from exc

    async def refund_star_payment(self, *, user_id: int, charge_id: str) -> None:
        try:
            done = await self._bot.refund_star_payment(
                user_id=user_id, telegram_payment_charge_id=charge_id
            )
        except TelegramAPIError as exc:
            raise BotApiError(redact(exc.message)) from exc
        if not done:
            raise BotApiError("refundStarPayment вернул False")

    async def cancel_star_subscription(self, *, user_id: int, charge_id: str) -> None:
        try:
            await self._bot.edit_user_star_subscription(
                user_id=user_id, telegram_payment_charge_id=charge_id, is_canceled=True
            )
        except TelegramAPIError as exc:
            raise BotApiError(redact(exc.message)) from exc

    async def send_text(self, chat_id: int, text: str) -> None:
        try:
            await self._bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        except TelegramAPIError as exc:
            raise BotApiError(redact(exc.message)) from exc
