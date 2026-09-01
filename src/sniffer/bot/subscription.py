"""Подписка со стороны бота: что показать, что записать после оплаты.

Отделено от `billing.py` намеренно. Там — правила Telegram и разбор оплаты, без
базы и без сети; здесь — единственное место, где подписка встречается с
хранилищем. Так `billing` проверяется обычными тестами, а этот модуль — на
живом Postgres, где и живут его настоящие свойства.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from sniffer.bot.billing import Purchase
from sniffer.db.engine import session_scope
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.db.repositories.listings import ListingRepository
from sniffer.db.repositories.passports import PassportRepository
from sniffer.db.repositories.users import UserRepository
from sniffer.domain.records import Payment, SubscriptionState

log = structlog.get_logger(__name__)


async def current_root(tg_user_id: int) -> int | None:
    """Корень цепочки паспорта, за которым клиент может начать следить.

    Нет паспорта — нечего и предлагать: подписка без темы это рассылка.
    """
    async with session_scope() as session:
        user = await UserRepository(session).get_or_create(tg_user_id)
        if user.id is None:  # pragma: no cover — репозиторий всегда возвращает id
            return None
        current = await PassportRepository(session).get_current(user.id)
        await session.commit()
        return None if current is None else (current.root_id or current.id)


async def owns(tg_user_id: int, passport_root: int) -> bool:
    """Тот ли это клиент. Проверка перед списанием, а не после.

    Строку `payload` формирует бот, но приходит она обратно от Telegram, и
    считать её доверенной нельзя (CLAUDE.md: любой внешний ввод недоверенный).
    Чужой корень цепочки означал бы подписку на чужой запрос за свой счёт.
    """
    async with session_scope() as session:
        user = await UserRepository(session).get_or_create(tg_user_id)
        if user.id is None:  # pragma: no cover
            return False
        found = await DeliveryRepository(session).owns_chain(
            user_id=user.id, passport_root=passport_root
        )
        await session.commit()
        return found


async def activate(tg_user_id: int, purchase: Purchase) -> SubscriptionState | None:
    """Зачислить оплату и включить подписку. `None` — платёж уже был учтён.

    Точка отсчёта — последняя карточка на момент оплаты. Без неё свежая подписка
    вываливает клиенту двухнедельный запас разом, включая ровно те объявления,
    которые он только что посмотрел и не выбрал.
    """
    async with session_scope() as session:
        user = await UserRepository(session).get_or_create(tg_user_id)
        if user.id is None:  # pragma: no cover
            return None
        delivery = DeliveryRepository(session)
        fresh = await delivery.pay_and_activate(
            _with_user(purchase.payment, user.id),
            passport_root=purchase.passport_root,
            until=purchase.until,
            since_listing_id=await ListingRepository(session).max_id(),
        )
        state = await delivery.subscription_for(
            user_id=user.id, passport_root=purchase.passport_root
        )
        await session.commit()
        if not fresh:
            log.info("billing.payment_already_seen", charge=purchase.payment.external_id)
            return None
        log.info(
            "billing.subscription_active",
            user=user.id,
            passport_root=purchase.passport_root,
            until=purchase.until.isoformat(),
        )
        return state


async def active_for(tg_user_id: int, passport_root: int) -> SubscriptionState | None:
    """Уже слежу? Нужно, чтобы не продавать второй раз одно и то же."""
    async with session_scope() as session:
        user = await UserRepository(session).get_or_create(tg_user_id)
        if user.id is None:  # pragma: no cover
            return None
        state = await DeliveryRepository(session).subscription_for(
            user_id=user.id, passport_root=passport_root
        )
        await session.commit()
        if state is None or state.expires_at is None:
            return None
        return state if state.expires_at > datetime.now(UTC) else None


def _with_user(payment: Payment, user_id: int) -> Payment:
    """Внутренний id клиента вместо телеграмного: в `payments` лежит наш.

    `billing` телеграмный id и знает — он приходит в апдейте, — но `users.id`
    ему взять неоткуда, а лезть за ним в базу из модуля без базы значило бы
    сломать ровно ту границу, ради которой он отделён.
    """
    return Payment(
        user_id=user_id,
        amount=payment.amount,
        external_id=payment.external_id,
        provider=payment.provider,
        currency=payment.currency,
        status=payment.status,
        is_recurring=payment.is_recurring,
    )
