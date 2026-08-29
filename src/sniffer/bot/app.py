"""Процесс бота: сборка диспетчера, long polling и вежливая остановка."""

from __future__ import annotations

import asyncio
from contextlib import suppress

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from sniffer.bot.handlers import search
from sniffer.config import Settings, get_settings
from sniffer.runtime.service import Service

log = structlog.get_logger(__name__)


def missing_settings(settings: Settings) -> list[str]:
    """Без токена бот не может даже поздороваться — это единственное требование."""
    return [] if settings.bot_token.strip() else ["BOT_TOKEN"]


def build_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(search.router)
    return dispatcher


async def run(stop: asyncio.Event) -> None:
    bot = Bot(
        get_settings().bot_token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            # Превью первой ссылки раздувает выдачу из пяти карточек в экран
            # чужих фотографий.
            link_preview_is_disabled=True,
        ),
    )
    dispatcher = build_dispatcher()

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
