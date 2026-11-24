"""Telegram client entry point: MCP onboarding and historical payment support."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from functools import cache

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ChatMemberUpdated,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardRemove,
)

from sniffer.bot import catalog_legacy
from sniffer.bot.handlers import membership
from sniffer.catalog_mcp.address import MCP_URL, PERSONAL_URL
from sniffer.config import Settings, get_settings
from sniffer.db.engine import session_scope
from sniffer.db.repositories.catalog_watches import CatalogWatchRepository
from sniffer.runtime.service import Service
from sniffer.telegram_env import bot_session

COMMANDS = (
    ("start", "Подключить каталог SnifferBot"),
    ("mcp", "Получить ссылку MCP"),
    ("paysupport", "Помощь по прежним платежам"),
)
INTRO = (
    "SnifferBot: каталог объявлений через MCP. Бесплатно, без подписки.\n\n"
    "Скопируйте адрес в настройки MCP вашего ассистента. "
    "Тип подключения: Streamable HTTP, без авторизации.\n\n"
    f"<code>{MCP_URL}</code>\n\n"
    "Поддерживается не каждым клиентом; например, в Claude соединение добавляется в веб.\n\n"
    "Личный ключ для рассылок: /mcp.\n\n"
    "Например: «Покажи квартиры в Нячанге до 6,5 млн донгов в месяц»."
)
PERSONAL_INTRO = (
    "\n\nДля бесплатных обновлений в этом Telegram подключите личный MCP:\n"
    f"<code>{PERSONAL_URL}</code>\n"
    "Заголовок Authorization: <code>Bearer {token}</code>\n"
    "Управляйте рассылками через create_watch, list_watches и stop_watch.\n"
    "Ключ приватный; не вставляйте его в чат ассистента, только в настройку заголовков. "
    "Новая команда /mcp заменяет предыдущий ключ; рассылки при этом сохраняются."
)
LEGACY_CHECKOUT_DISABLED = "Новые подписки не оформляются. По прежним платежам: /paysupport."


@cache
def dispatcher() -> Dispatcher:
    router = Router(name="catalog-entry")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(catalog_legacy.support, Command("paysupport"))
    # Only historical payment events; no invoice, offer, manual refund or recurring charge
    # initiation. A late charge is journaled and invalid receipts require manual handling.
    router.message.register(catalog_legacy.record_receipt, F.successful_payment)
    router.message.register(catalog_legacy.record_provider_refund, F.refunded_payment)
    router.subscription.register(catalog_legacy.record_provider_status)

    @router.pre_checkout_query()
    async def reject_checkout(query: PreCheckoutQuery) -> None:
        await query.answer(ok=False, error_message=LEGACY_CHECKOUT_DISABLED)

    @router.my_chat_member(F.chat.type == ChatType.PRIVATE)
    async def owner_membership(event: ChatMemberUpdated) -> None:
        if get_settings().is_owner(event.chat.id):
            await membership.bot_reachability(event)

    @router.callback_query()
    async def stale_button(callback: CallbackQuery) -> None:
        await callback.answer("Эта кнопка устарела. Откройте /mcp.", show_alert=True)

    @router.message(Command("mcp"))
    async def personal_connection(message: Message) -> None:
        sender = message.from_user
        if sender is None or sender.is_bot or sender.id != message.chat.id:
            return
        try:
            async with asyncio.timeout(4), session_scope() as session:
                token = await CatalogWatchRepository(session).issue(sender.id)
                await session.commit()
        except Exception:
            text = INTRO + "\n\nЛичный ключ пока недоступен. Повторите /mcp позже."
        else:
            text = INTRO + PERSONAL_INTRO.format(token=token)
        await message.answer(text, reply_markup=ReplyKeyboardRemove())

    @router.message()
    async def introduction(message: Message) -> None:
        await message.answer(INTRO, reply_markup=ReplyKeyboardRemove())

    root = Dispatcher()
    root.include_router(router)
    return root


def missing_settings(settings: Settings) -> list[str]:
    return [] if settings.bot_token.strip() else ["BOT_TOKEN"]


async def run(stop: asyncio.Event) -> None:
    settings = get_settings()
    bot = Bot(
        settings.bot_token,
        session=bot_session(settings),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    async with bot.context():
        # A failed menu write must not keep the new /start and /mcp unavailable.
        with suppress(TelegramAPIError):
            await bot.set_my_commands(
                [BotCommand(command=name, description=text) for name, text in COMMANDS]
            )
        root = dispatcher()
        poll = asyncio.create_task(
            root.start_polling(bot, handle_signals=False, close_bot_session=False)
        )
        stopping = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait({poll, stopping}, return_when=asyncio.FIRST_COMPLETED)
            if poll not in done:
                try:
                    await root.stop_polling()
                except RuntimeError:
                    poll.cancel()
            with suppress(asyncio.CancelledError):
                await poll
        finally:
            stopping.cancel()
            if not poll.done():
                poll.cancel()
                with suppress(asyncio.CancelledError):
                    await poll


SERVICE = Service(name="bot", requires=missing_settings, run=run)
