"""Подписки, очередь доставки и защита от повторов.

Три таблицы в одном репозитории, потому что это один агрегат: подписка решает,
кому слать, `notifications` помнит, что уже слали, а `outbox` держит то, что
ещё не ушло. Разнести их по трём классам значило бы открывать три транзакции
там, где нужна одна: отметка «отправлено» и постановка в очередь обязаны
случиться вместе, иначе перезапуск воркера шлёт карточку второй раз.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Table, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sniffer.db import models
from sniffer.db.mappers import to_stored_passport
from sniffer.db.repositories.base import Repository
from sniffer.domain.records import OutboxMessage, SubscriptionState

OUTBOX_PENDING = "pending"
OUTBOX_SENT = "sent"
OUTBOX_FAILED = "failed"


class DeliveryRepository(Repository):
    async def active_subscriptions(self, *, limit: int = 200) -> list[SubscriptionState]:
        """Живые подписки вместе с ТЕКУЩЕЙ версией паспорта.

        Подписка хранит корень цепочки, а не версию: клиент правит запрос, и
        подписка обязана следовать за правкой, а не застывать на той версии,
        при которой её создали. Отсюда join по `COALESCE(root_id, id)`.
        """
        chain = func.coalesce(models.Passport.root_id, models.Passport.id)
        rows = await self._session.execute(
            select(models.Subscription, models.Passport)
            .join(models.Passport, chain == models.Subscription.passport_root)
            .where(
                models.Subscription.is_active.is_(True),
                models.Passport.is_current.is_(True),
            )
            .order_by(models.Subscription.id)
            .limit(limit)
        )
        return [_subscription(row, passport) for row, passport in rows]

    async def enqueue(
        self,
        *,
        subscription_id: int,
        user_id: int,
        listing_id: int,
        score: float,
        payload: dict[str, Any],
        scheduled_at: datetime | None = None,
    ) -> bool:
        """Поставить карточку в очередь и запомнить, что она отправлена.

        Обе записи одной транзакцией и в этом порядке. `ON CONFLICT DO NOTHING`
        по `(subscription_id, listing_id)` — не перестраховка: воркер идёт
        пачками и встретит ту же карточку снова, а `False` в ответе честно
        означает «уже было», а не ошибку.

        `scheduled_at` ставится явно, а не колонкой `DEFAULT now()`. Разница
        видна не сразу: со значением по умолчанию время постановки берётся с
        часов БАЗЫ, то есть проход не может ни отложить доставку (дайджест на
        вечер), ни быть проверен на заданном времени — он зависит от того, что
        показывают чужие часы в момент вставки.
        """
        table = cast(Table, models.Notification.__table__)
        noted = await self._session.execute(
            pg_insert(table)
            .values(subscription_id=subscription_id, listing_id=listing_id, score=score)
            .on_conflict_do_nothing(index_elements=["subscription_id", "listing_id"])
            .returning(table.c.id)
        )
        if noted.scalar_one_or_none() is None:
            return False
        self._session.add(
            models.Outbox(
                user_id=user_id,
                payload=payload,
                scheduled_at=scheduled_at or datetime.now(UTC),
            )
        )
        await self._session.flush()
        return True

    async def take_pending(
        self, *, limit: int = 20, now: datetime | None = None
    ) -> list[OutboxMessage]:
        """Что пора доставить. `SKIP LOCKED` — чтобы две копии не слали дважды."""
        rows = await self._session.scalars(
            select(models.Outbox)
            .where(
                models.Outbox.status == OUTBOX_PENDING,
                models.Outbox.scheduled_at <= (now or datetime.now(UTC)),
            )
            .order_by(models.Outbox.scheduled_at, models.Outbox.id)
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
        return [_outbox(row) for row in rows]

    async def mark_sent(self, message_id: int, *, now: datetime | None = None) -> None:
        await self._session.execute(
            update(models.Outbox)
            .where(models.Outbox.id == message_id)
            .values(status=OUTBOX_SENT, sent_at=now or datetime.now(UTC))
        )

    async def mark_failed(self, message_id: int, *, retry_at: datetime) -> None:
        """Не ушло — вернуть в очередь позже, счётчик попыток вверх.

        Статус остаётся `pending`: `failed` означал бы «больше не пробуем», а
        недоступный Telegram — причина подождать, а не выбросить карточку.
        """
        await self._session.execute(
            update(models.Outbox)
            .where(models.Outbox.id == message_id)
            .values(attempts=models.Outbox.attempts + 1, scheduled_at=retry_at)
        )

    async def give_up(self, message_id: int) -> None:
        """Попытки исчерпаны. Единственное место, где ставится `failed`."""
        await self._session.execute(
            update(models.Outbox).where(models.Outbox.id == message_id).values(status=OUTBOX_FAILED)
        )

    async def sent_since(self, subscription_id: int, *, since: datetime) -> int:
        """Сколько ушло по подписке с этого момента — суточный лимит.

        Считаем запросом по `notifications`, а не колонкой `sent_today`:
        счётчик требует не забыть обнулить его в полночь, запрос — не требует
        ничего. Колонка в схеме остаётся, но источником правды не служит.

        Границу передаёт вызывающий, и это не придирка. Первая версия сравнивала
        `date(sent_at)` с датой — а `date()` от `timestamptz` считается в
        часовом поясе СЕССИИ базы. Суточный лимит клиента сбрасывался бы в
        полночь того пояса, в котором подняли Postgres, и никакой тест этого не
        показал бы, пока пояс не сменят.
        """
        return int(
            await self._session.scalar(
                select(func.count(models.Notification.id)).where(
                    models.Notification.subscription_id == subscription_id,
                    models.Notification.sent_at >= since,
                )
            )
            or 0
        )


def _subscription(row: models.Subscription, passport: models.Passport) -> SubscriptionState:
    return SubscriptionState(
        id=row.id,
        user_id=row.user_id,
        passport_root=row.passport_root,
        mode=row.mode,
        max_per_day=row.max_per_day,
        quiet_from=row.quiet_from,
        quiet_to=row.quiet_to,
        passport=to_stored_passport(passport),
    )


def _outbox(row: models.Outbox) -> OutboxMessage:
    return OutboxMessage(
        id=row.id,
        user_id=row.user_id,
        payload=dict(row.payload),
        attempts=row.attempts,
        scheduled_at=row.scheduled_at,
    )
