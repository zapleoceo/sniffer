"""Подписки, очередь доставки и защита от повторов.

Три таблицы в одном репозитории, потому что это один агрегат: подписка решает,
кому слать, `notifications` помнит, что уже слали, а `outbox` держит то, что
ещё не ушло. Разнести их по трём классам значило бы открывать три транзакции
там, где нужна одна: отметка «отправлено» и постановка в очередь обязаны
случиться вместе, иначе перезапуск воркера шлёт карточку второй раз.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    DateTime,
    Table,
    and_,
    exists,
    func,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import REAL
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sniffer.db import models
from sniffer.db.mappers import to_subscription_state
from sniffer.db.repositories.base import Repository
from sniffer.domain.records import OutboxMessage, Payment, SubscriptionState

OUTBOX_PENDING = "pending"
OUTBOX_SENT = "sent"
OUTBOX_FAILED = "failed"
# Сообщение отменено, а не потеряно: право на него кончилось раньше, чем оно ушло.
OUTBOX_CANCELLED = "cancelled"
# Почему строка отменена: пишется в `outbox.last_error` и читается человеком.
REASON_EXPIRED = "expired"


def entitled(now: datetime) -> ColumnElement[bool]:
    """Подписка вправе получать карточки в момент `now`: включена и оплачена по этот момент.

    ОДИН предикат права на всё, что делает монитор: выбор подписок на проход
    (`MonitorRepository.claim_due`) и постановка в очередь (`enqueue`) спрашивают его, а не
    свои копии условия. Условие с копиями разъезжается тихо: выбор отсёк просроченную, а
    постановка, не знавшая про срок, всё равно поставила бы карточку.

    Срок проверяется прямо в запросе, а не отдельным сторожем, который «должен» вовремя
    выключить подписку: пропущенный проход сторожа означал бы бесплатную рассылку, а
    пропущенное условие в запросе — ничего не означает, его просто нет.

    `expires_at IS NULL` читается как «бессрочно»: так заведены подписки без платежа
    (владелец, ручная выдача). Платёж срок ставит всегда. Заменит этот предикат право,
    считаемое от числа живых подписок Stars (пакет биллинга), — менять его надо здесь,
    в одном месте.
    """
    return and_(
        models.Subscription.is_active.is_(True),
        or_(models.Subscription.expires_at.is_(None), models.Subscription.expires_at > now),
    )


class DeliveryRepository(Repository):
    async def advance_scan(self, subscription_id: int, listing_id: int) -> None:
        """Монотонно запомнить последнюю рассмотренную карточку."""
        await self._session.execute(
            update(models.Subscription)
            .where(models.Subscription.id == subscription_id)
            .values(scan_listing_id=func.greatest(models.Subscription.scan_listing_id, listing_id))
        )

    async def enqueue(
        self,
        *,
        subscription_id: int,
        user_id: int,
        listing_id: int,
        score: float,
        payload: dict[str, Any],
        scheduled_at: datetime | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Поставить карточку в очередь и запомнить, что она отправлена.

        `False` — карточка не поставлена: либо она уже была в очереди этой подписки, либо
        подписка в момент `now` не вправе получать (истекла, на паузе). В обоих случаях
        в базе не появляется ни строки.

        Обе записи одной транзакцией и в этом порядке. `ON CONFLICT DO NOTHING`
        по `(subscription_id, listing_id)` — не перестраховка: воркер идёт
        пачками и встретит ту же карточку снова, а `False` в ответе честно
        означает «уже было», а не ошибку.

        `scheduled_at` ставится явно, а не колонкой `DEFAULT now()`. Разница
        видна не сразу: со значением по умолчанию время постановки берётся с
        часов БАЗЫ, то есть проход не может ни отложить доставку (дайджест на
        вечер), ни быть проверен на заданном времени — он зависит от того, что
        показывают чужие часы в момент вставки.

        Тем же правилом `now` задаёт и `notifications.created_at`. Суточный слот
        занимает именно он, а проход считает остаток слотов от СВОЕЙ полуночи:
        слот со временем с часов базы и граница суток по часам прохода — это два
        «сейчас», и при проходе на заданном времени лимит считался бы по чужому.
        """
        moment = now or datetime.now(UTC)
        table = cast(Table, models.Notification.__table__)
        # Право проверяется в самой вставке, а не запросом перед ней: между «проверил» и
        # «вставил» подписка успела бы истечь или встать на паузу. Выбор подписок на проход
        # уже спрашивал тот же предикат, и здесь он не лишний: постановка — единственное
        # место, где карточка становится обязательством перед клиентом, и она не вправе
        # доверять тому, что вызывающий когда-то проверил (D7).
        still_entitled = select(
            literal(subscription_id, BigInteger),
            literal(listing_id, BigInteger),
            literal(score, REAL),
            literal(moment, DateTime(timezone=True)),
        ).where(exists().where(models.Subscription.id == subscription_id, entitled(moment)))
        noted = await self._session.execute(
            pg_insert(table)
            .from_select(["subscription_id", "listing_id", "score", "created_at"], still_entitled)
            .on_conflict_do_nothing(index_elements=["subscription_id", "listing_id"])
            .returning(table.c.id)
        )
        notification_id = noted.scalar_one_or_none()
        if notification_id is None:
            return False
        self._session.add(
            models.Outbox(
                user_id=user_id,
                subscription_id=subscription_id,
                notification_id=notification_id,
                payload=payload,
                scheduled_at=scheduled_at or moment,
            )
        )
        await self._session.flush()
        return True

    async def take_pending(
        self, *, limit: int = 20, now: datetime | None = None
    ) -> list[OutboxMessage]:
        """Что пора доставить — только прочитать, без блокировки.

        Блокировка здесь жила, пока вся пачка шла одной транзакцией: строки
        были заперты на всё время прохода, а проход с паузой в секунду между
        сообщениями — это десятки секунд. Теперь отправка идёт по одному
        сообщению, и запирает строки `lock_pending` — ровно на время одной отправки.
        """
        rows = await self._session.execute(
            select(models.Outbox, models.User.tg_user_id)
            .join(models.User, models.User.id == models.Outbox.user_id)
            .where(
                models.Outbox.status == OUTBOX_PENDING,
                models.Outbox.scheduled_at <= (now or datetime.now(UTC)),
            )
            .order_by(models.Outbox.scheduled_at, models.Outbox.id)
            .limit(limit)
        )
        return [_outbox(row, tg_user_id) for row, tg_user_id in rows]

    async def lock_pending(self, ids: Sequence[int], *, now: datetime) -> list[OutboxMessage]:
        """Запереть строки одной отправки. `SKIP LOCKED` — чтобы две копии не слали дважды.

        Перечитываем по `id`, а не доверяем тому, что вернул `take_pending`: пока
        проход дошёл до этой строки, другая копия могла её отправить или отложить.
        Поэтому условия повторены: ушедшая и отложенная строки сюда не попадают, а
        занятая чужой транзакцией пропускается, а не ожидается.
        """
        if not ids:
            return []
        rows = await self._session.execute(
            select(models.Outbox, models.User.tg_user_id)
            .join(models.User, models.User.id == models.Outbox.user_id)
            .where(
                models.Outbox.id.in_(list(ids)),
                models.Outbox.status == OUTBOX_PENDING,
                models.Outbox.scheduled_at <= now,
            )
            .order_by(models.Outbox.scheduled_at, models.Outbox.id)
            .with_for_update(of=models.Outbox, skip_locked=True)
        )
        return [_outbox(row, tg_user_id) for row, tg_user_id in rows]

    async def mark_sent(self, message_id: int, *, now: datetime | None = None) -> None:
        moment = now or datetime.now(UTC)
        notification_id = await self._session.scalar(
            select(models.Outbox.notification_id).where(models.Outbox.id == message_id)
        )
        await self._session.execute(
            update(models.Outbox)
            .where(models.Outbox.id == message_id)
            .values(status=OUTBOX_SENT, sent_at=moment, last_error=None)
        )
        if notification_id is not None:
            await self._session.execute(
                update(models.Notification)
                .where(models.Notification.id == notification_id)
                .values(sent_at=moment)
            )

    async def mark_failed(
        self, message_id: int, *, retry_at: datetime, error: str | None = None
    ) -> None:
        """Не ушло — вернуть в очередь позже, счётчик попыток вверх.

        Статус остаётся `pending`: `failed` означал бы «больше не пробуем», а
        недоступный Telegram — причина подождать, а не выбросить карточку.
        """
        await self._session.execute(
            update(models.Outbox)
            .where(models.Outbox.id == message_id)
            .values(
                attempts=models.Outbox.attempts + 1,
                scheduled_at=retry_at,
                last_error=error,
            )
        )

    async def give_up(self, message_id: int, *, error: str | None = None) -> None:
        """Больше не пробуем: Telegram отказал сообщению или попытки кончились.

        Попытка засчитана и здесь: по `attempts` потом видно, сколько раз мы
        стучались, а не «на одну меньше». Статус `failed` — это отказ, а решение
        не слать (просрочено, клиент заблокировал бота) называется `cancelled`.
        """
        await self._session.execute(
            update(models.Outbox)
            .where(models.Outbox.id == message_id)
            .values(
                status=OUTBOX_FAILED,
                attempts=models.Outbox.attempts + 1,
                last_error=error,
            )
        )

    async def cancel_pending_of(self, user_id: int, *, reason: str) -> int:
        """Отменить всё, что ждёт отправки этому клиенту. Возврат — сколько строк.

        Вызывается, когда Telegram сказал, что писать клиенту нельзя: остальные
        его строки отправятся с тем же отказом, и каждая такая попытка — лишний
        запрос к Bot API. Отменяем, а не `failed`: это наше решение не слать.
        """
        return await self._cancel(models.Outbox.user_id == user_id, reason=reason)

    async def cancel_for_blocked_users(self, *, reason: str) -> int:
        """Отменить очередь тех, кто заблокировал бота, — в том числе строки, поставленные позже.

        Метку ставит и нотифаер (по 403), и бот (по `my_chat_member`), а очередь
        наполняют и матчер, и сборщик отложенных ответов: ни один из них не
        обязан помнить про блокировку. Проход нотифаера подчищает за всеми.
        """
        blocked = select(models.User.id).where(models.User.bot_blocked_at.is_not(None))
        return await self._cancel(models.Outbox.user_id.in_(blocked), reason=reason)

    async def cancel_expired(self, *, now: datetime, ttl: timedelta) -> int:
        """Отменить строки, которые слать уже поздно. Возврат — сколько строк отменено.

        Возраст строки — от времени, на которое она назначена (`scheduled_at`), а
        не от постановки: подборка на вечерние 18:00 созревает вечером, и вчерашней
        она становится от этого часа, а не от утра, когда её поставили. Повтор после
        сбоя сдвигает `scheduled_at` вперёд, но попыток конечное число, так что
        застрявшая строка всё равно упрётся в потолок попыток.

        Одно правило срока годности. Отмену по окончании подписки (шесть часов
        льготы) делает проход матчера — `MonitorRepository.cancel_lapsed`: правило
        «шесть часов после срока» живёт в одном месте, а не в двух с разными числами.
        """
        return await self._cancel(models.Outbox.scheduled_at < now - ttl, reason=REASON_EXPIRED)

    async def _cancel(self, *conditions: Any, reason: str) -> int:
        # `SKIP LOCKED`: строку, которую нотифаер уже взял в отправку, отмена не ждёт —
        # судьба такой строки решена (отменять отправляемое поздно), а ожидание
        # блокировки стояло бы на пути всего прохода.
        pending = (
            select(models.Outbox.id)
            .where(models.Outbox.status == OUTBOX_PENDING, *conditions)
            .with_for_update(skip_locked=True)
        )
        done = await self._session.execute(
            update(models.Outbox)
            .where(models.Outbox.id.in_(pending))
            .values(status=OUTBOX_CANCELLED, last_error=reason)
            .returning(models.Outbox.id)
            .execution_options(synchronize_session=False)
        )
        return len(done.all())

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

    async def used_since(self, subscription_id: int, *, since: datetime) -> int:
        """Сколько суточных слотов уже занято, включая ожидающие доставки."""
        return int(
            await self._session.scalar(
                select(func.count(models.Notification.id)).where(
                    models.Notification.subscription_id == subscription_id,
                    models.Notification.created_at >= since,
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
        return to_subscription_state(row[0], row[1]) if row is not None else None

    async def set_active(
        self, *, user_id: int, passport_root: int, active: bool, now: datetime | None = None
    ) -> bool:
        """Поставить мониторинг на паузу или возобновить оплаченный."""
        changed = await self._session.execute(
            update(models.Subscription)
            .where(
                models.Subscription.user_id == user_id,
                models.Subscription.passport_root == passport_root,
                models.Subscription.expires_at > (now or datetime.now(UTC)),
            )
            .values(is_active=active)
            .returning(models.Subscription.id)
        )
        return changed.scalar_one_or_none() is not None


def _outbox(row: models.Outbox, recipient_id: int) -> OutboxMessage:
    return OutboxMessage(
        id=row.id,
        user_id=row.user_id,
        recipient_id=recipient_id,
        payload=dict(row.payload),
        attempts=row.attempts,
        scheduled_at=row.scheduled_at,
        subscription_id=row.subscription_id,
        notification_id=row.notification_id,
    )
