"""Деньги в терминах предметной области: виды платежа, строка журнала, событие.

Без ввода-вывода и без Telegram: репозиторий (`db/`) и сервис оплаты (`bot/`)
договариваются через эти типы, а не через ORM-модели и не через объекты aiogram.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

# Статус платежа в журнале (`payments.status`). Движется только вперёд:
# `paid` → `refunding` → `refunded`; повторная доставка исходного апдейта ничего не
# воскрешает. `refunding` пишется ДО вызова Telegram: иначе сообщение о возврате,
# прилетевшее раньше нашей отметки, выглядело бы как возврат «не нашими руками».
# Слот такой платёж уже не держит: решение вернуть принято.
PAID = "paid"
REFUNDING = "refunding"
REFUNDED = "refunded"
# Откуда запись о платеже (`payments.source`).
FROM_UPDATE = "update"
FROM_RECONCILE = "reconcile"


class PaymentKind(StrEnum):
    """Что за платёж пришёл (`payments.kind`).

    `unknown` — не наш счёт, не та сумма или валюта: такой платёж возвращается
    сам. Остальные виды из схемы (`one_off`, `duplicate`) заводят пакеты, которым
    они нужны; здесь только то, что эта версия умеет отличать.
    """

    FIRST = "first"
    RENEWAL = "renewal"
    UNKNOWN = "unknown"


class Reason(StrEnum):
    """Почему платёж или счёт не принят. Клиенту каждая причина говорится своими словами."""

    LEGACY_INVOICE = "legacy_invoice"
    BAD_PAYLOAD = "bad_payload"
    FOREIGN_BUYER = "foreign_buyer"
    WRONG_CURRENCY = "wrong_currency"
    WRONG_AMOUNT = "wrong_amount"
    NOT_A_SUBSCRIPTION = "not_a_subscription"
    NO_CONSENT = "no_consent"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"


class EventKind(StrEnum):
    """Что записано в журнале событий оплаты (`billing_events.kind`)."""

    SUB_CANCELED = "sub_canceled"
    SUB_ACTIVE = "sub_active"
    SUB_FAILED = "sub_failed"
    SUB_OTHER = "sub_other"
    REFUNDED = "refunded"
    SUPPORT = "support"
    # Сверка нашла расхождение и сказала о нём владельцу: по записи — один раз.
    RECONCILE_GAP = "reconcile_gap"
    # Сверка не смогла довести возврат и сказала об этом: по платежу — один раз.
    REFUND_STUCK = "refund_stuck"
    # Режим `report`: сверка нашла, что сделала бы в режиме `refund`, и только сообщила.
    RECONCILE_REPORT = "reconcile_report"


@dataclass(frozen=True, slots=True)
class PaymentRecord:
    """Платёж, который надо записать в журнал: всё, что прислал Telegram, и наш вердикт."""

    charge_id: str
    tg_user_id: int
    amount: int
    currency: str
    kind: PaymentKind
    invoice_payload: str
    is_recurring: bool
    is_first_recurring: bool
    period_end: datetime | None
    raw: dict[str, Any]
    source: str = FROM_UPDATE
    # Срок посчитан нами (сверка), а не взят у Telegram.
    period_end_estimated: bool = False


@dataclass(frozen=True, slots=True)
class StoredPayment:
    """Строка журнала, как она лежит в базе."""

    charge_id: str
    tg_user_id: int | None
    amount: int
    currency: str
    kind: str | None
    status: str
    invoice_payload: str | None
    is_recurring: bool
    is_first_recurring: bool
    period_end: datetime | None
    refunded_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BillingEvent:
    """Событие без своего идентификатора платежа: изменение подписки, обращение."""

    kind: EventKind
    tg_user_id: int
    payload: dict[str, Any]
    charge_id: str | None = None
    update_id: int | None = None


@dataclass(frozen=True, slots=True)
class StarTransaction:
    """Строка истории звёзд бота у Telegram (`getStarTransactions`): то, с чем сверяется журнал."""

    charge_id: str
    amount: int
    date: datetime
    # Входящая — платёж клиента; исходящая с тем же id — наш возврат.
    incoming: bool
    user_id: int | None = None
    invoice_payload: str | None = None
    subscription_period: int | None = None
