"""Поиски клиента: список, выбор и управление мониторингом.

Заголовок поиска здесь не живёт: он нужен и там, где меню нет (сообщение о
вытесненном поиске), поэтому лежит в `bot/threads.py` вместе с остальным знанием
о поисках.

Два разных вопроса, и путать их нельзя. «Что показать в списке» — `list_for`, и
ответ обрезан пределом. «Принадлежит ли этот поиск клиенту и что с ним» —
`get_one`, и ответ от списка не зависит: вытесненный из списка поиск остаётся
поиском клиента, и его мониторинг идёт и платится независимо от того, влез ли он
в пять строк.
"""

from __future__ import annotations

from dataclasses import dataclass

from sniffer.bot.store import Client
from sniffer.db.engine import session_scope
from sniffer.db.repositories import PassportRepository, UserRepository
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.domain.records import QueryOverview


@dataclass(frozen=True, slots=True)
class Menu:
    """Список поисков и то, к чему относится следующее сообщение человека."""

    items: list[QueryOverview]
    # Человек сказал `/new`: следующее сообщение откроет новый поиск, а не уточнит
    # отмеченный. Пока это так, «✓» в списке врал бы.
    starting_new: bool = False


async def list_for(client: Client) -> Menu:
    async with session_scope() as session:
        user = await UserRepository(session).get_or_create(
            client.tg_user_id, username=client.username
        )
        rows = [] if user.id is None else await PassportRepository(session).list_queries(user.id)
        await session.commit()
        return Menu(items=rows, starting_new=user.awaiting_new_request)


async def get_one(client: Client, root: int) -> QueryOverview | None:
    """Поиск клиента по корню или `None`, если корень чужой или несуществующий."""
    async with session_scope() as session:
        user = await UserRepository(session).get_or_create(
            client.tg_user_id, username=client.username
        )
        found = (
            None if user.id is None else await PassportRepository(session).get_query(user.id, root)
        )
        await session.commit()
        return found


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
