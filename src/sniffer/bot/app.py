"""Процесс бота: сборка диспетчера, long polling и вежливая остановка."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from functools import cache

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand

from sniffer.bot.billing_wording import COMMANDS
from sniffer.bot.handlers import billing, search
from sniffer.config import Settings, get_settings
from sniffer.runtime.service import Service
from sniffer.telegram_env import bot_session

log = structlog.get_logger(__name__)


def missing_settings(settings: Settings) -> list[str]:
    """Без токена бот не может даже поздороваться — это единственное требование."""
    return [] if settings.bot_token.strip() else ["BOT_TOKEN"]


@cache
def build_dispatcher() -> Dispatcher:
    """Диспетчер процесса. Кэш: роутеры модульные и прикрепляются к родителю один раз —
    второй вызов упал бы с «Router is already attached», а процесс собирает его один раз."""
    dispatcher = Dispatcher()
    # Порядок значим: оплата ДО диалога. `@router.message(F.text)` диалога ловит всё, в том
    # числе команды, и без этого порядка `/paysupport` — обязательная по ToS Telegram —
    # уходила бы в поиск как поисковая фраза.
    dispatcher.include_router(billing.router)
    dispatcher.include_router(search.router)
    return dispatcher


async def publish_commands(bot: Bot) -> None:
    """Меню команд: без него `/terms` и `/paysupport` есть, но никто о них не знает.

    Не получилось — не повод не стартовать: меню обновится при следующем запуске.
    """
    commands = [BotCommand(command=name, description=text) for name, text in COMMANDS]
    try:
        await bot.set_my_commands(commands)
    except TelegramAPIError as exc:
        log.warning("bot.commands_not_published", error=type(exc).__name__)


async def run(stop: asyncio.Event) -> None:
    settings = get_settings()
    bot = Bot(
        settings.bot_token,
        session=bot_session(settings),
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            # Превью первой ссылки раздувает выдачу из пяти карточек в экран
            # чужих фотографий.
            link_preview_is_disabled=True,
        ),
    )
    dispatcher = build_dispatcher()
    await publish_commands(bot)

    # handle_signals=False: сигналами владеет runtime. Два обработчика на один
    # SIGTERM — это гонка за то, кто первым закроет сессию.
    polling = asyncio.create_task(dispatcher.start_polling(bot, handle_signals=False))
    stopping = asyncio.create_task(stop.wait())
    log.info("bot.polling")

    done, _ = await asyncio.wait({polling, stopping}, return_when=asyncio.FIRST_COMPLETED)
    stopping.cancel()
    if polling not in done:
        try:
            await dispatcher.stop_polling()
        except RuntimeError:
            # SIGTERM пришёл раньше, чем опрос успел начаться: останавливать
            # ещё нечего, но задачу надо снять.
            polling.cancel()
    # Ошибку опроса поднимаем наружу: молча продолжать без диалога незачем.
    with suppress(asyncio.CancelledError):
        await polling


SERVICE = Service(name="bot", requires=missing_settings, run=run)
