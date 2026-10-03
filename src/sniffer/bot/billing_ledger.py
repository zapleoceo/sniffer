"""Журнал платежей на базе: единственное место, где оплата встречается с хранилищем.

Адаптер протокола `Ledger` (`billing_ports.py`). Каждый метод — одна короткая единица
работы со своей сессией и явным коммитом: журнал пишется первым делом, и держать
транзакцию открытой на время вызова Telegram нельзя. Весь SQL — в репозитории.
"""

from __future__ import annotations

from datetime import timedelta

from sniffer.db.engine import session_scope
from sniffer.db.repositories.billing import BillingRepository
from sniffer.db.repositories.users import UserRepository
from sniffer.domain.billing import BillingEvent, EventKind, PaymentRecord, StoredPayment


class DbLedger:
    async def record_payment(self, record: PaymentRecord) -> bool:
        async with session_scope() as session:
            # Клиент мог ни разу не писать боту текстом (ссылку открыли с другого
            # устройства): платёж от этого не должен остаться без строки в журнале.
            user = await UserRepository(session).get_or_create(record.tg_user_id)
            if user.id is None:  # pragma: no cover — репозиторий всегда возвращает id
                raise LookupError(f"у клиента {record.tg_user_id} нет внутреннего id")
            fresh = await BillingRepository(session).insert_payment(user.id, record)
            await session.commit()
            return fresh

    async def get_payment(self, charge_id: str) -> StoredPayment | None:
        async with session_scope() as session:
            return await BillingRepository(session).get_payment(charge_id)

    async def recent_payments(self, tg_user_id: int, limit: int) -> list[StoredPayment]:
        async with session_scope() as session:
            return await BillingRepository(session).recent_payments(tg_user_id, limit=limit)

    async def mark_refunded(self, charge_id: str) -> bool:
        async with session_scope() as session:
            changed = await BillingRepository(session).mark_refunded(charge_id)
            await session.commit()
            return changed

    async def first_charge_of(self, invoice_payload: str) -> str | None:
        async with session_scope() as session:
            return await BillingRepository(session).first_charge_of(invoice_payload)

    async def live_subscriptions(self, tg_user_id: int) -> int:
        async with session_scope() as session:
            user = await UserRepository(session).get_by_tg_id(tg_user_id)
            if user is None or user.id is None:
                return 0
            return await BillingRepository(session).live_subscriptions(user.id)

    async def record_consent(self, tg_user_id: int, doc: str, version: str) -> None:
        async with session_scope() as session:
            user = await UserRepository(session).get_or_create(tg_user_id)
            if user.id is None:  # pragma: no cover — репозиторий всегда возвращает id
                raise LookupError(f"у клиента {tg_user_id} нет внутреннего id")
            await BillingRepository(session).record_consent(user.id, doc, version)
            await session.commit()

    async def has_consent(self, tg_user_id: int, doc: str, version: str) -> bool:
        async with session_scope() as session:
            user = await UserRepository(session).get_by_tg_id(tg_user_id)
            if user is None or user.id is None:
                return False
            return await BillingRepository(session).has_consent(user.id, doc, version)

    async def record_event(self, event: BillingEvent) -> bool:
        async with session_scope() as session:
            recorded = await BillingRepository(session).record_event(event)
            await session.commit()
            return recorded

    async def events_within(self, tg_user_id: int, kind: EventKind, window: timedelta) -> int:
        async with session_scope() as session:
            return await BillingRepository(session).events_within(tg_user_id, kind, window)
