"""Число слотов слежения клиента из журнала платежей: боевая реализация порта `Slots`.

Слот = живая подписка Stars (30 суток, автопродление). Считаем тем же запросом, что раскладка
сроков по мониторингам (`BillingRepository.live_subscriptions`), поэтому число и сроки не
расходятся. Сессия своя и короткая, только на чтение: проход монитора держит свою.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db.engine import session_scope
from sniffer.db.repositories.billing import BillingRepository

Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class LedgerSlots:
    def __init__(self, sessions: Sessions = session_scope) -> None:
        self._sessions = sessions

    async def count(self, user_id: int, now: datetime) -> int | None:
        async with self._sessions() as session:
            return await BillingRepository(session).live_subscriptions(user_id, now)
