"""Журнал оплаты звёздами: платежи, согласие с условиями, события подписки.

Репозиторий не знает ни про Telegram, ни про то, что считать «своим» платежом:
он пишет и читает то, что решил сервис оплаты. Идемпотентность держится
уникальными ключами (`payments.external_id`, `billing_events.update_id`, первичный
ключ согласия) и `ON CONFLICT DO NOTHING`, а не проверкой «нет ли уже такого»:
между проверкой и вставкой у Telegram, повторяющего апдейт, остаётся окно.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import Table, and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sniffer.db import models
from sniffer.db.repositories.base import Repository
from sniffer.domain.billing import (
    PAID,
    REFUNDED,
    REFUNDING,
    BillingEvent,
    EventKind,
    PaymentKind,
    PaymentRecord,
    StoredPayment,
)


class BillingRepository(Repository):
    async def insert_payment(self, user_id: int, record: PaymentRecord) -> bool:
        """Записать платёж в журнал. `False` — этот `charge_id` уже был.

        Платёж пишется первым делом и ДО всего остального: если дальше что-то
        упадёт, строка уже лежит, а в `raw` — всё, что прислал Telegram, и по
        ней платёж можно разобрать и вернуть вручную.
        """
        table = cast(Table, models.Payment.__table__)
        inserted = await self._session.execute(
            pg_insert(table)
            .values(
                user_id=user_id,
                tg_user_id=record.tg_user_id,
                amount=record.amount,
                currency=record.currency,
                status=PAID,
                external_id=record.charge_id,
                invoice_payload=record.invoice_payload,
                kind=record.kind.value,
                is_recurring=record.is_recurring,
                is_first_recurring=record.is_first_recurring,
                period_end=record.period_end,
                period_end_estimated=record.period_end_estimated,
                source=record.source,
                raw=record.raw,
            )
            .on_conflict_do_nothing(index_elements=["external_id"])
            .returning(table.c.id)
        )
        return inserted.scalar_one_or_none() is not None

    async def get_payment(self, charge_id: str) -> StoredPayment | None:
        row = (
            await self._session.execute(
                select(models.Payment, models.User.tg_user_id)
                .join(models.User, models.User.id == models.Payment.user_id)
                .where(models.Payment.external_id == charge_id)
            )
        ).first()
        return None if row is None else _stored(row[0], row[1])

    async def recent_payments(self, tg_user_id: int, *, limit: int = 3) -> list[StoredPayment]:
        """Последние платежи клиента, свежие сверху: контекст для обращения в поддержку."""
        rows = await self._session.execute(
            select(models.Payment, models.User.tg_user_id)
            .join(models.User, models.User.id == models.Payment.user_id)
            .where(models.User.tg_user_id == tg_user_id)
            .order_by(models.Payment.created_at.desc(), models.Payment.id.desc())
            .limit(limit)
        )
        return [_stored(payment, tg_id) for payment, tg_id in rows]

    async def mark_refunding(self, charge_id: str) -> bool:
        """Решено вернуть: пишется ДО вызова Telegram. `False` — платёж уже не `paid`.

        Слот такой платёж больше не держит (живые подписки считают только `paid`), а
        сообщение о возврате, пришедшее раньше нашей отметки «возвращён», опознаётся как
        наше по этому статусу.
        """
        changed = await self._session.execute(
            update(models.Payment)
            .where(models.Payment.external_id == charge_id, models.Payment.status == PAID)
            .values(status=REFUNDING)
            .returning(models.Payment.id)
        )
        return changed.scalar_one_or_none() is not None

    async def mark_refunded(self, charge_id: str) -> bool:
        """Платёж возвращён. `False` — он уже был помечен или его нет в журнале.

        Статус движется только вперёд (`paid` → `refunded`), поэтому повторная
        доставка исходного платежа его не воскрешает: `insert_payment` на
        существующий `charge_id` ничего не меняет.
        """
        changed = await self._session.execute(
            update(models.Payment)
            .where(models.Payment.external_id == charge_id, models.Payment.status != REFUNDED)
            .values(status=REFUNDED, refunded_at=func.now())
            .returning(models.Payment.id)
        )
        return changed.scalar_one_or_none() is not None

    async def first_charge_of(self, invoice_payload: str) -> str | None:
        """Самый ранний платёж подписки: его id — ключ `editUserStarSubscription`."""
        charge: str | None = await self._session.scalar(
            select(models.Payment.external_id)
            .where(models.Payment.invoice_payload == invoice_payload)
            .order_by(models.Payment.created_at, models.Payment.id)
            .limit(1)
        )
        return charge

    async def first_payment_of(self, invoice_payload: str) -> StoredPayment | None:
        """Самый ранний платёж подписки (по записи в журнале), со всеми полями."""
        row = (
            await self._session.execute(
                select(models.Payment, models.User.tg_user_id)
                .join(models.User, models.User.id == models.Payment.user_id)
                .where(models.Payment.invoice_payload == invoice_payload)
                .order_by(models.Payment.created_at, models.Payment.id)
                .limit(1)
            )
        ).first()
        return None if row is None else _stored(row[0], row[1])

    async def payments_since(self, since: datetime) -> list[StoredPayment]:
        rows = await self._session.execute(
            select(models.Payment, models.User.tg_user_id)
            .join(models.User, models.User.id == models.Payment.user_id)
            .where(models.Payment.created_at >= since)
            .order_by(models.Payment.created_at, models.Payment.id)
        )
        return [_stored(payment, tg_id) for payment, tg_id in rows]

    async def unsettled_refunds(self, older_than: datetime) -> list[StoredPayment]:
        """Возврат решён, но не доведён: `refunding` либо «не наш» платёж, оставшийся `paid`.

        Падение процесса между записью «не нашего» платежа и вызовом возврата оставляет
        именно такую строку; без этого запроса она жила бы вечно. `older_than` не даёт
        сверке перехватить возврат, который прямо сейчас делает обработчик апдейта.
        """
        rows = await self._session.execute(
            select(models.Payment, models.User.tg_user_id)
            .join(models.User, models.User.id == models.Payment.user_id)
            .where(
                models.Payment.created_at < older_than,
                or_(
                    models.Payment.status == REFUNDING,
                    and_(
                        models.Payment.status == PAID,
                        models.Payment.kind == PaymentKind.UNKNOWN.value,
                    ),
                ),
            )
            .order_by(models.Payment.created_at, models.Payment.id)
        )
        return [_stored(payment, tg_id) for payment, tg_id in rows]

    async def has_event(self, kind: EventKind, charge_id: str) -> bool:
        found = await self._session.scalar(
            select(models.BillingEvent.id)
            .where(
                models.BillingEvent.kind == kind.value, models.BillingEvent.charge_id == charge_id
            )
            .limit(1)
        )
        return found is not None

    async def live_period_ends(self, user_id: int, now: datetime) -> list[datetime]:
        """Сроки живых подписок клиента, самый долгий первым: по одному на подписку.

        Подписка — это счёт (`invoice_payload`); срок — максимум `period_end` по её платежам
        со статусом `paid`, поэтому возврат последнего продления сам укорачивает срок, а
        платёж, который решено вернуть (`refunding`), слота уже не держит. «Сейчас»
        приходит от вызывающего, а не с часов базы: пересчёт слотов и квота обязаны
        видеть один и тот же момент.
        """
        ends = (
            select(func.max(models.Payment.period_end).label("ends"))
            .where(
                models.Payment.user_id == user_id,
                models.Payment.invoice_payload.is_not(None),
                models.Payment.kind.in_([PaymentKind.FIRST.value, PaymentKind.RENEWAL.value]),
                models.Payment.status == PAID,
            )
            .group_by(models.Payment.invoice_payload)
            .subquery()
        )
        rows = await self._session.scalars(
            select(ends.c.ends).where(ends.c.ends > now).order_by(ends.c.ends.desc())
        )
        return list(rows)

    async def live_subscriptions(self, user_id: int, now: datetime) -> int:
        """Сколько подписок клиента оплачено в момент `now` — число слотов.

        Ровно длина списка сроков: число слотов и сроки, которые раскладываются по
        мониторингам, не могут разойтись, потому что считаются одним запросом.
        """
        return len(await self.live_period_ends(user_id, now))

    async def record_consent(self, user_id: int, doc: str, version: str) -> None:
        """Клиент согласился с этой версией документа. Повтор ничего не меняет."""
        table = cast(Table, models.UserConsent.__table__)
        await self._session.execute(
            pg_insert(table)
            .values(user_id=user_id, doc=doc, version=version)
            .on_conflict_do_nothing(index_elements=["user_id", "doc", "version"])
        )

    async def has_consent(self, user_id: int, doc: str, version: str) -> bool:
        found = await self._session.scalar(
            select(models.UserConsent.user_id).where(
                models.UserConsent.user_id == user_id,
                models.UserConsent.doc == doc,
                models.UserConsent.version == version,
            )
        )
        return found is not None

    async def record_event(self, event: BillingEvent) -> bool:
        """Записать событие. `False` — апдейт с таким `update_id` уже был."""
        table = cast(Table, models.BillingEvent.__table__)
        inserted = await self._session.execute(
            pg_insert(table)
            .values(
                update_id=event.update_id,
                kind=event.kind.value,
                tg_user_id=event.tg_user_id,
                charge_id=event.charge_id,
                payload=event.payload,
            )
            .on_conflict_do_nothing(index_elements=["update_id"])
            .returning(table.c.id)
        )
        return inserted.scalar_one_or_none() is not None

    async def events_within(self, tg_user_id: int, kind: EventKind, window: timedelta) -> int:
        """Сколько событий этого вида было у клиента за последнее `window`.

        Окно считает база (`now()`), а не часы приложения: так же, как срок подписки.
        """
        return int(
            await self._session.scalar(
                select(func.count(models.BillingEvent.id)).where(
                    models.BillingEvent.tg_user_id == tg_user_id,
                    models.BillingEvent.kind == kind.value,
                    models.BillingEvent.created_at >= func.now() - window,
                )
            )
            or 0
        )


def _stored(row: models.Payment, tg_user_id: int) -> StoredPayment:
    return StoredPayment(
        charge_id=row.external_id,
        tg_user_id=row.tg_user_id if row.tg_user_id is not None else tg_user_id,
        amount=row.amount,
        currency=row.currency,
        kind=row.kind,
        status=row.status,
        invoice_payload=row.invoice_payload,
        is_recurring=row.is_recurring,
        is_first_recurring=row.is_first_recurring,
        period_end=row.period_end,
        refunded_at=row.refunded_at,
        created_at=row.created_at,
    )
