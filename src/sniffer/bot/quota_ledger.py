"""Журнал показов поверх Postgres: единственное место, где бот встречает базу ради квоты.

Каждый вызов — своя сессия и свой коммит. Резерв обязан быть зафиксирован до
отправки сообщения (иначе параллельный ответ не увидит занятых слотов), поэтому
коммит здесь, а не у вызывающего: он про диалог, а не про границы транзакций.
Отделено от `quota.py`, чтобы тот проверялся без SQLAlchemy.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.bot.quota import Account, QuotaService
from sniffer.bot.store import Client
from sniffer.config import get_settings
from sniffer.db.engine import session_scope
from sniffer.db.repositories.quota import QuotaRepository
from sniffer.db.repositories.users import UserRepository
from sniffer.domain.quota import Claim, Reserved, Ticket, Usage

Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class SqlLedger:
    def __init__(self, sessions: Sessions = session_scope) -> None:
        self._sessions = sessions

    async def reserve(self, claim: Claim) -> Reserved:
        async with self._sessions() as session:
            reserved = await QuotaRepository(session).reserve(claim)
            await session.commit()
            return reserved

    async def confirm(self, ticket: Ticket, at: datetime) -> None:
        async with self._sessions() as session:
            await QuotaRepository(session).confirm(ticket, at=at)
            await session.commit()

    async def release(self, ticket: Ticket) -> None:
        async with self._sessions() as session:
            await QuotaRepository(session).release(ticket)
            await session.commit()

    async def usage(self, user_id: int, now: datetime) -> Usage:
        async with self._sessions() as session:
            return await QuotaRepository(session).usage(user_id, now)

    async def identify(self, refs: Sequence[tuple[str, str]]) -> dict[tuple[str, str], int]:
        async with self._sessions() as session:
            return await QuotaRepository(session).identify(refs)

    async def claim_offer(self, user_id: int, now: datetime, cooldown: timedelta) -> bool:
        async with self._sessions() as session:
            claimed = await UserRepository(session).claim_paywall_offer(
                user_id, now=now, cooldown=cooldown
            )
            await session.commit()
            return claimed

    async def sweep(self, older_than: datetime, limit: int) -> int:
        async with self._sessions() as session:
            removed = await QuotaRepository(session).sweep_stale(older_than=older_than, limit=limit)
            await session.commit()
            return removed


async def account_of(client: Client, sessions: Sessions = session_scope) -> Account:
    """Аккаунт для квоты: наш id по телеграмному (клиент заводится, если пишет впервые)."""
    async with sessions() as session:
        user = await UserRepository(session).get_or_create(
            client.tg_user_id, username=client.username
        )
        await session.commit()
    if user.id is None:  # pragma: no cover — репозиторий возвращает вставленную строку
        raise LookupError(f"клиент {client.tg_user_id} без id")
    return Account(user_id=user.id, tg_user_id=client.tg_user_id)


def new_quota() -> QuotaService:
    """Квота поверх Postgres в том виде, в каком её собирают процессы.

    Владелец (`OWNER_CHAT_ID`) квоты не знает; пустое значение — «владелец не задан».
    Одна фабрика на бота и на отложенные ответы: две сборки разошлись бы в том, кого
    считать владельцем.
    """
    return QuotaService(SqlLedger(), owner_tg_id=get_settings().owner_chat_id or None)
