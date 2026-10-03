"""Ветки запросов клиента: список, выбор и управление мониторингом.

Заголовок ветки здесь не живёт: он нужен и там, где меню нет (сообщение о
вытесненной ветке), поэтому лежит в `bot/threads.py` вместе с остальным знанием
о ветках.
"""

from __future__ import annotations

from sniffer.bot.store import Client
from sniffer.db.engine import session_scope
from sniffer.db.repositories import PassportRepository, UserRepository
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.domain.records import QueryOverview


async def list_for(client: Client) -> list[QueryOverview]:
    async with session_scope() as session:
        user = await UserRepository(session).get_or_create(
            client.tg_user_id, username=client.username
        )
        rows = [] if user.id is None else await PassportRepository(session).list_queries(user.id)
        await session.commit()
        return rows


async def select(client: Client, root: int, *, editing: bool = False) -> bool:
    async with session_scope() as session:
        user = await UserRepository(session).get_or_create(
            client.tg_user_id, username=client.username
        )
        changed = bool(
            user.id is not None
            and await PassportRepository(session).select(user.id, root, editing=editing)
        )
        await session.commit()
        return changed


async def toggle(client: Client, root: int, *, active: bool) -> bool:
    async with session_scope() as session:
        user = await UserRepository(session).get_or_create(
            client.tg_user_id, username=client.username
        )
        changed = bool(
            user.id is not None
            and await DeliveryRepository(session).set_active(
                user_id=user.id, passport_root=root, active=active
            )
        )
        await session.commit()
        return changed
