"""Жизненный цикл тем: создать, привязать, переименовать. Темы — представление поиска.

Ни одна функция здесь не бросает наружу ошибку Bot API: тема — удобство, а не условие
работы поиска. Отказ создания или переименования уходит в журнал, а человек продолжает
работать так же, как без тем (General и `/watch`). Удаление темы человеком Telegram не
сообщает, поэтому «поиск жив, а темы нет» — штатное состояние, и чинит его нотифаер
(`mark_lost`) и повторное «Вкладка» в панели.

`deleteForumTopic` не вызывается никогда: он стирает историю темы без отката, в том числе
карточки, по которым человек сверялся (R6 §1.6, п. 5). «Удалить поиск» архивирует, но темы
не трогает.
"""

from __future__ import annotations

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from sniffer.bot import watch_flow
from sniffer.bot.store import Client, PassportStore
from sniffer.bot.threads import title
from sniffer.db.engine import session_scope
from sniffer.db.repositories import UserRepository
from sniffer.db.repositories.tabs import TabRepository

log = structlog.get_logger(__name__)

# Имя темы — до 128 знаков (Bot API), но в списке тем длинное имя обрезается клиентом.
TOPIC_NAME_LIMIT = 64
NEW_TOPIC_NAME = "Новый поиск"


async def create_blank(bot: Bot, client: Client) -> bool:
    """Пустая тема под новый поиск: связь появится с первым сообщением человека в ней."""
    try:
        await bot.create_forum_topic(chat_id=client.tg_user_id, name=NEW_TOPIC_NAME)
    except TelegramAPIError as exc:
        log.warning("topics.create_failed", error=type(exc).__name__)
        return False
    return True


async def open_tab(bot: Bot, client: Client, root: int) -> str | None:
    """Тема для существующего поиска. Возврат — имя темы или `None`, если не вышло."""
    item = await watch_flow.overview(client, root)
    if item is None:
        return None
    name = title(item.passport, limit=TOPIC_NAME_LIMIT)
    try:
        topic = await bot.create_forum_topic(chat_id=client.tg_user_id, name=name)
    except TelegramAPIError as exc:
        log.warning("topics.create_failed", error=type(exc).__name__)
        return None
    async with session_scope() as session:
        user = await UserRepository(session).get_or_create(
            client.tg_user_id, username=client.username
        )
        if user.id is None:  # pragma: no cover — репозиторий возвращает вставленную строку
            return None
        attached = await TabRepository(session).attach(user.id, root, topic.message_thread_id, name)
        await session.commit()
    return name if attached else None


async def sync_title(bot: Bot, client: Client, store: PassportStore | None = None) -> None:
    """Имя темы следует за названием поиска: сменился предмет или город — переименовать."""
    if client.thread_id is None:
        return
    dialogue = await (store or PassportStore()).load(client)
    if dialogue.passport is None:
        return
    name = title(dialogue.passport.passport, limit=TOPIC_NAME_LIMIT)
    async with session_scope() as session:
        tabs = TabRepository(session)
        known = await tabs.tab_of(dialogue.user_id, dialogue.passport.root)
        if known is None or known[1] == name:
            return
        try:
            await bot.edit_forum_topic(
                chat_id=client.tg_user_id, message_thread_id=client.thread_id, name=name
            )
        except TelegramAPIError as exc:
            log.warning("topics.rename_failed", error=type(exc).__name__)
            return
        await tabs.set_title(dialogue.user_id, dialogue.passport.root, name)
        await session.commit()
