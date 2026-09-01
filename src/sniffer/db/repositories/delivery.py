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

from sqlalchemy import Table, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sniffer.db import models
from sniffer.db.mappers import to_stored_passport
from sniffer.db.repositories.base import Repository
from sniffer.domain.records import OutboxMessage, Payment, SubscriptionState

OUTBOX_PENDING = "pending"
OUTBOX_SENT = "sent"
OUTBOX_FAILED = "failed"


class DeliveryRepository(Repository):
    async def active_subscriptions(
        self, *, limit: int = 200, now: datetime | None = None
    ) -> list[SubscriptionState]:
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
                # Оплачена по сегодня. Проверка здесь, а не отдельным сторожем,
                # который «должен» вовремя выключить подписку: пропущенный
                # проход такого сторожа означал бы бесплатную рассылку, а
                # пропущенное условие в запросе — ничего не означает, его
                # просто нет.
                or_(
                    models.Subscription.expires_at.is_(None),
                    models.Subscription.expires_at > (now or datetime.now(UTC)),
                ),
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

    async def owns_chain(self, *, user_id: int, passport_root: int) -> bool:
        """Принадлежит ли эта цепочка паспортов этому клиенту.

        Строку счёта формирует бот, но приходит она обратно от Telegram, и
        доверенной не является (CLAUDE.md: любой внешний ввод недоверенный).
        Без этой проверки подделанный `payload` подписал бы клиента на чужой
        запрос за его же деньги.
        """
        chain = func.coalesce(models.Passport.root_id, models.Passport.id)
        return bool(
            await self._session.scalar(
                select(models.Passport.id)
                .where(chain == passport_root, models.Passport.user_id == user_id)
                .limit(1)
            )
        )

    async def pay_and_activate(
        self, payment: Payment, *, passport_root: int, until: datetime, since_listing_id: int
    ) -> bool:
        """Платёж → активная подписка. Возврат `False` — платёж уже был учтён.

        Одной транзакцией и в одном месте, потому что здесь встречаются деньги
        и доступ: записать платёж без подписки значит взять звезду и ничего не
        дать, включить подписку без платежа — раздать бесплатно.

        Идемпотентность держится на `payments.external_id UNIQUE`, а не на
        проверке «а нет ли уже такого»: Telegram повторяет апдейт при любой
        задержке ответа, и проверка отдельным запросом оставляет окно между ней
        и вставкой. `ON CONFLICT DO NOTHING` окна не оставляет.
        """
        table = cast(Table, models.Payment.__table__)
        inserted = await self._session.execute(
            pg_insert(table)
            .values(
                user_id=payment.user_id,
                amount=payment.amount,
                currency=payment.currency,
                provider=payment.provider,
                status=payment.status,
                external_id=payment.external_id,
                is_recurring=payment.is_recurring,
            )
            .on_conflict_do_nothing(index_elements=["external_id"])
            .returning(table.c.id)
        )
        payment_id = inserted.scalar_one_or_none()
        if payment_id is None:
            # Повторная доставка того же апдейта. Подписку не трогаем: она уже
            # продлена этим самым платежом.
            return False

        subscription = cast(Table, models.Subscription.__table__)
        row = await self._session.execute(
            pg_insert(subscription)
            .values(
                user_id=payment.user_id,
                passport_root=passport_root,
                is_active=True,
                expires_at=until,
                charge_id=payment.external_id,
                since_listing_id=since_listing_id,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "passport_root"],
                # Продление: срок и ключ платежа обновляются, точка отсчёта —
                # НЕТ. Иначе повторная оплата сдвигала бы её на «сейчас», и
                # клиент терял бы всё, что накопилось за оплаченный месяц.
                set_={
                    "is_active": True,
                    "expires_at": until,
                    "charge_id": payment.external_id,
                },
            )
            .returning(subscription.c.id)
        )
        await self._session.execute(
            update(table).where(table.c.id == payment_id).values(subscription_id=row.scalar_one())
        )
        return True

    async def subscription_for(
        self, *, user_id: int, passport_root: int
    ) -> SubscriptionState | None:
        chain = func.coalesce(models.Passport.root_id, models.Passport.id)
        found = await self._session.execute(
            select(models.Subscription, models.Passport)
            .join(models.Passport, chain == models.Subscription.passport_root)
            .where(
                models.Subscription.user_id == user_id,
                models.Subscription.passport_root == passport_root,
                models.Passport.is_current.is_(True),
            )
            .limit(1)
        )
        row = found.first()
        return _subscription(row[0], row[1]) if row is not None else None


def _subscription(row: models.Subscription, passport: models.Passport) -> SubscriptionState:
    return SubscriptionState(
        id=row.id,
        user_id=row.user_id,
        passport_root=row.passport_root,
        mode=row.mode,
        max_per_day=row.max_per_day,
        quiet_from=row.quiet_from,
        quiet_to=row.quiet_to,
        since_listing_id=row.since_listing_id,
        expires_at=row.expires_at,
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
