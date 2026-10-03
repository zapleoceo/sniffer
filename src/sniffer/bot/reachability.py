"""Доступен ли клиенту бот: узнаём из апдейтов, а не отправкой.

Telegram присылает `my_chat_member`, когда клиент блокирует бота (статус `kicked`)
или снимает блокировку (`member`). Без этого о блокировке узнавали первым же 403
на карточке — холостым запросом, — а о снятии не узнавали вовсе: метка, поставленная
по 403, висела бы, пока клиент сам не напишет.

Второй путь снятия нужен потому, что апдейт может потеряться (бот лежал дольше суток,
а Telegram хранит неполученное ровно столько) или обработаться не в том порядке:
блокировку и снятие, пришедшие одной пачкой, обрабатывают разные задачи. Клиент,
заплативший за слежение, молчал бы навсегда. Поэтому любое сообщение или нажатие
клиента тоже снимает метку: написать боту, который его заблокировал, нельзя.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from sniffer.db.engine import session_scope
from sniffer.db.repositories.users import UserRepository

log = structlog.get_logger(__name__)


async def record(tg_user_id: int, *, blocked: bool, at: datetime) -> None:
    """Запомнить блокировку или снять метку. Коммит только когда есть что менять."""
    async with session_scope() as session:
        changed = await UserRepository(session).set_bot_blocked(tg_user_id, blocked=blocked, at=at)
        if changed is not None:
            await session.commit()
    if changed is not None:
        log.info("bot.reachability", user=changed, blocked=blocked)


class MarkReachable(BaseMiddleware):
    """Сообщение или нажатие клиента доказывает, что бот ему доступен.

    Вешается на сообщения и нажатия, но не на `my_chat_member`: апдейт о блокировке
    тоже «от клиента», и middleware снял бы метку сразу после того, как её поставили.
    Снятие пишет в строку, только когда метка стоит, — на каждое сообщение это один
    запрос по уникальному ключу без записи.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is not None:
            await record(user.id, blocked=False, at=datetime.now(UTC))
        return await handler(event, data)
