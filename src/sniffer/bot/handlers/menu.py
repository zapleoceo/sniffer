"""Кнопки постоянной клавиатуры: каждая зовёт то же действие, что и команда.

Один путь, без копии логики: хендлеры команд (`/new`, `/requests`, `/plan`, `/subscription`,
`/help`) вызываются как есть. Роутер стоит в диспетчере ДО диалога (`bot/app.py`): подпись
кнопки приходит обычным текстом, и общий текстовый обработчик принял бы её за поисковый запрос.
Фильтр — точное равенство текста, а не вхождение: «подписка на скутер» остаётся поиском.
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject
from aiogram.types import Message

from sniffer.bot import wording
from sniffer.bot.handlers import billing, search

router = Router(name="menu")


@router.message(F.text == wording.BTN_NEW)
async def new_search(message: Message) -> None:
    await search.new_request(message, CommandObject(prefix="/", command="new"))


@router.message(F.text == wording.BTN_REQUESTS)
async def requests(message: Message) -> None:
    await search.requests(message)


@router.message(F.text == wording.BTN_PLAN)
async def plan(message: Message) -> None:
    await search.plan(message)


@router.message(F.text == wording.BTN_SUBSCRIPTION)
async def subscription(message: Message, bot: Bot) -> None:
    await billing.subscription_command(message, bot)


@router.message(F.text == wording.BTN_HELP)
async def help_(message: Message) -> None:
    await search.help_command(message)
