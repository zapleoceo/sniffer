"""Журнал показов поверх Postgres: единственное место, где бот встречает базу ради квоты.

Каждый вызов — своя сессия и свой коммит. Резерв обязан быть зафиксирован до
отправки сообщения (иначе параллельный ответ не увидит занятых слотов), поэтому
коммит здесь, а не у вызывающего: он про диалог, а не про границы транзакций.
Отделено от `quota.py`, чтобы тот проверялся без SQLAlchemy.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db.engine import session_scope
from sniffer.db.repositories.quota import QuotaRepository
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

    async def claim_offer(self, user_id: int, now: datetime, cooldown: timedelta) -> bool:
        async with self._sessions() as session:
            claimed = await QuotaRepository(session).claim_offer(
                user_id, now=now, cooldown=cooldown
            )
            await session.commit()
            return claimed

    async def sweep(self, older_than: datetime, limit: int) -> int:
        async with self._sessions() as session:
            removed = await QuotaRepository(session).sweep_stale(older_than=older_than, limit=limit)
            await session.commit()
            return removed
