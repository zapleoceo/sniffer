"""Клиент заблокировал бота или снял блокировку: апдейт `my_chat_member`.

Хендлер тонкий: решает, блокировка это или нет, и отдаёт запись `reachability`.
Что с этим делать дальше, решают те, кто читает метку: матчер перестаёт ставить
клиенту в очередь, нотифаер отменяет ждущие строки (docs/architecture.md, «Доставка»).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import ChatMemberUpdated

from sniffer.bot import reachability

router = Router(name="membership")


@router.my_chat_member(F.chat.type == ChatType.PRIVATE)
async def bot_reachability(event: ChatMemberUpdated) -> None:
    """В личном чате апдейт приходит только о блокировке и её снятии.

    Группы отсеяны фильтром: бот там не работает, и добавление его в чужой чат не
    должно помечать «заблокировавшим» человека, который его добавил.
    """
    status = event.new_chat_member.status
    if status == ChatMemberStatus.KICKED:
        blocked = True
    elif status == ChatMemberStatus.MEMBER:
        blocked = False
    else:
        return
    # В личном чате id чата — это id клиента.
    await reachability.record(event.chat.id, blocked=blocked, at=event.date)
