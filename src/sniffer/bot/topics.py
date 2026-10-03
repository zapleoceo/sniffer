"""Темы Telegram: включён ли режим и откуда берётся тема клиента.

Режим включается в два шага, и оба обязательны: флаг `TOPICS_ENABLED` (выключатель на случай,
когда молодой API снова сломает отправку в темы) и самопроверка при старте
`getMe().has_topics_enabled` (Threaded mode в @BotFather включает владелец). Нет любого —
бот работает как без тем: `Client.thread_id` пуст, нотифаер шлёт без темы.

Состояние живёт в модуле, а не в базе: оно про текущий процесс бота и его токен, а не про
клиента. Проверка — единственное место, где оно меняется (`check`).
"""

from __future__ import annotations

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from sniffer.bot.store import Client
from sniffer.config import Settings

log = structlog.get_logger(__name__)

_on = False


def active() -> bool:
    return _on


def set_active(value: bool) -> None:
    global _on
    _on = value


async def check(bot: Bot, settings: Settings) -> bool:
    """Самопроверка при старте. Любой отказ означает «без тем», а не падение процесса."""
    if not settings.topics_enabled:
        set_active(False)
        return False
    try:
        me = await bot.get_me()
    except TelegramAPIError as exc:
        log.warning("topics.self_check_failed", error=type(exc).__name__)
        set_active(False)
        return False
    set_active(bool(me.has_topics_enabled))
    if not me.has_topics_enabled:
        log.warning("topics.not_enabled_in_botfather")
    return active()


def thread_of(message: Message) -> int | None:
    """Тема сообщения; без темы, в General и при выключенном режиме — `None`."""
    if active() and message.is_topic_message and message.message_thread_id is not None:
        return message.message_thread_id
    return None


def client_of_message(message: Message) -> Client | None:
    if message.from_user is None:
        return None
    return Client(message.from_user.id, message.from_user.username, thread_of(message))


def client_of_callback(callback: CallbackQuery, message: Message) -> Client:
    return Client(callback.from_user.id, callback.from_user.username, thread_of(message))
