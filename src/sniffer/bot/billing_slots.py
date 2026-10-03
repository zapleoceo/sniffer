"""Слоты мониторинга на базе: адаптер портов `Slots` и `Entitlements`, включение и перенос.

Единственное место бота, где слоты встречаются с хранилищем. Каждый вызов — своя сессия и
явный коммит: пересчёт слотов случается сразу после записи платежа, и держать его в одной
транзакции с вызовами Telegram нельзя. Весь SQL — в `db/repositories/slots.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.bot.quota import Account
from sniffer.db.engine import session_scope
from sniffer.db.repositories.billing import BillingRepository
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.db.repositories.slots import SlotRepository
from sniffer.db.repositories.users import UserRepository
from sniffer.domain.slots import Monitor, Outcome, SlotState

Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class DbSlots:
    def __init__(self, sessions: Sessions = session_scope) -> None:
        self._sessions = sessions

    async def _user_id(self, session: AsyncSession, tg_user_id: int) -> int:
        user = await UserRepository(session).get_or_create(tg_user_id)
        if user.id is None:  # pragma: no cover — репозиторий всегда возвращает id
            raise LookupError(f"у клиента {tg_user_id} нет внутреннего id")
        return user.id

    async def sync(self, tg_user_id: int, now: datetime) -> SlotState:
        async with self._sessions() as session:
            user_id = await self._user_id(session, tg_user_id)
            state = await SlotRepository(session).sync(user_id, now)
            await session.commit()
            return state

    async def enable(self, tg_user_id: int, root: int, now: datetime) -> Outcome:
        """Включить слежение на ветке клиента. Чужая ветка — как «слота нет»: не включается."""
        async with self._sessions() as session:
            user_id = await self._user_id(session, tg_user_id)
            owns = await DeliveryRepository(session).owns_chain(user_id=user_id, passport_root=root)
            if not owns:
                return Outcome.NO_FREE_SLOT
            outcome, _ = await SlotRepository(session).enable(user_id, root, now)
            await session.commit()
            return outcome

    async def move(self, tg_user_id: int, *, to_root: int, from_root: int, now: datetime) -> bool:
        async with self._sessions() as session:
            user_id = await self._user_id(session, tg_user_id)
            owns = await DeliveryRepository(session).owns_chain(
                user_id=user_id, passport_root=to_root
            )
            if not owns:
                return False
            moved = await SlotRepository(session).move(
                user_id, to_root=to_root, from_root=from_root, now=now
            )
            await session.commit()
            return moved

    async def holders(self, tg_user_id: int, now: datetime) -> list[Monitor]:
        """Мониторинги, которые сейчас держат слот: из них клиент выбирает, откуда переносить."""
        async with self._sessions() as session:
            user_id = await self._user_id(session, tg_user_id)
            monitors = await SlotRepository(session).monitors(user_id)
            return sorted(
                (monitor for monitor in monitors if monitor.holds_slot(now)),
                key=lambda monitor: (monitor.priority, monitor.id),
            )


class LedgerEntitlements:
    """Право тарифа для квоты карточек: число живых подписок из журнала платежей."""

    def __init__(self, sessions: Sessions = session_scope) -> None:
        self._sessions = sessions

    async def slots(self, account: Account, now: datetime) -> int:
        async with self._sessions() as session:
            return await BillingRepository(session).live_subscriptions(account.user_id, now)
