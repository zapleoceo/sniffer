"""Правила оплаты звёздами без базы и сети: счёт версии v2, проверки, разбор платежа.

Чистый модуль: ни базы, ни Telegram, ни часов (время приходит параметром). Поэтому
деньги проверяются обычными тестами, а не на живом Postgres и не руками в клиенте.

Три места, где ошибиться дорого.

**Счёт — только ссылка.** Подписку Telegram принимает через `createInvoiceLink`
(параметр `subscription_period` есть у него и нет у `sendInvoice`), поэтому нагрузка
счёта строится здесь, а сам вызов — в сервисе. Нагрузка v2 несёт id покупателя,
версию условий, с которой он согласился, и случайную соль: она уникальна на каждую
ссылку, а значит, по ней различаются подписки одного человека.

**Личность плательщика — из апдейта, а не из нагрузки.** Ссылку на счёт может
открыть кто угодно, поэтому `pre_checkout_query.from` сверяется с id в нагрузке:
пересланная ссылка не продаёт чужую подписку чужому аккаунту.

**Сумма и валюта сверяются с одной константой.** Старый счёт на прежнюю цену после
смены тарифа остаётся оплачиваемым по ней, пока `pre_checkout` не сравнивает
`total_amount` с `domain/plans.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sniffer.domain import plans
from sniffer.domain.billing import FROM_UPDATE, PaymentKind, Reason, StoredPayment

PAYLOAD_VERSION = "v2"
# Ограничение Telegram на нагрузку счёта, байт.
MAX_PAYLOAD_BYTES = 128

# Явные классы [0-9], а не `\d` и не `str.isdigit()`: для «²» и арабо-индийских цифр
# `isdigit()` истинно, а `int()` падает ValueError — в окне `pre_checkout` это
# неотвеченный запрос и сорванный платёж. `fullmatch`, а не `match` с `$`: `$`
# пропускает хвостовой перевод строки.
_PAYLOAD = re.compile(
    PAYLOAD_VERSION + r":s:([1-9][0-9]{0,19}):([0-9]{4}-[0-9]{2}-[0-9]{2}):([0-9a-f]{12})"
)
# Счета-сообщения прежней модели («1 ⭐ за тему») остались в чатах с кнопкой Pay.
_LEGACY = re.compile(r"sub:[0-9]+")
# Id клиента в команде владельца — тоже явными [0-9], без нуля впереди.
_TG_ID = re.compile(r"[1-9][0-9]{0,19}")
MAX_CHARGE_ID = 256


@dataclass(frozen=True, slots=True)
class InvoicePayload:
    """Нагрузка счёта: для кого, с какой версией условий и какая именно ссылка."""

    tg_user_id: int
    terms_version: str
    nonce: str

    def __post_init__(self) -> None:
        # Всё, что мы выдаём, мы же обязаны принять назад: иначе опечатка в версии или
        # в соли тихо превращается в «счёт устарел» на первой же оплате.
        if _PAYLOAD.fullmatch(self.encode()) is None:
            raise ValueError(f"нагрузка счёта не разбирается: {self.encode()!r}")

    def encode(self) -> str:
        return f"{PAYLOAD_VERSION}:s:{self.tg_user_id}:{self.terms_version}:{self.nonce}"


def parse_payload(raw: str) -> InvoicePayload | Reason:
    """Нагрузка из апдейта либо причина, по которой счёт не наш. Не падает ни на каком вводе."""
    found = _PAYLOAD.fullmatch(raw)
    if found is None:
        return Reason.LEGACY_INVOICE if _LEGACY.fullmatch(raw) else Reason.BAD_PAYLOAD
    return InvoicePayload(int(found.group(1)), found.group(2), found.group(3))


def parse_refund_args(args: str) -> tuple[str, int | None] | None:
    """`/refund <charge_id> [id клиента]` → разобранные аргументы либо `None`, если команда кривая.

    Второй аргумент нужен для платежа, которого нет в журнале (запись не удалась): без
    id клиента Telegram такой платёж не вернуть. Идентификатор платежа Telegram не
    описывает, поэтому форму не угадываем — только отсекаем пустое, длинное и непечатное.
    """
    parts = args.split()
    if len(parts) not in (1, 2):
        return None
    charge = parts[0]
    if len(charge) > MAX_CHARGE_ID or not charge.isprintable():
        return None
    if len(parts) == 1:
        return charge, None
    return (charge, int(parts[1])) if _TG_ID.fullmatch(parts[1]) else None


@dataclass(frozen=True, slots=True)
class CheckoutFacts:
    """Что известно в `pre_checkout_query`: деньги ещё не сняты."""

    buyer_id: int
    currency: str
    total_amount: int
    payload: str


@dataclass(frozen=True, slots=True)
class PaymentFacts:
    """Что известно в `successful_payment`: деньги уже сняты."""

    payer_id: int | None
    currency: str
    total_amount: int
    payload: str
    charge_id: str
    expiration: int | None
    is_recurring: bool
    is_first_recurring: bool
    raw: dict[str, Any]
    source: str = FROM_UPDATE
    # Срок в `expiration` придуман сверкой (дата платежа + период), а не прислан Telegram.
    expiration_estimated: bool = False


@dataclass(frozen=True, slots=True)
class RefundedFacts:
    """Что известно в сообщении `refunded_payment`: возврат уже случился."""

    payer_id: int | None
    charge_id: str
    total_amount: int
    payload: str
    update_id: int | None = None


def check_buyer(buyer_id: int | None, payload: InvoicePayload) -> Reason | None:
    """Платит тот, для кого выписан счёт. Id берётся из апдейта, а не из нагрузки."""
    return None if buyer_id == payload.tg_user_id else Reason.FOREIGN_BUYER


def check_price(currency: str, amount: int) -> Reason | None:
    """Валюта и сумма — те, что в тарифе СЕЙЧАС: новый счёт и первый платёж подписки."""
    return check_amount(currency, amount, plans.SUBSCRIPTION_STARS)


def check_amount(currency: str, amount: int, expected: int | None) -> Reason | None:
    """Сверка с заданной суммой. У продления это сумма ПЕРВОГО платежа этой подписки.

    Цена подписки фиксируется при выписке счёта, и после смены тарифа продление старого
    подписчика законно идёт по прежней цене: сверка с текущей вернула бы честный платёж
    как «не наш». `None` — сверять нечем (журнал не ответил): сумма не проверяется,
    валюта по-прежнему да.
    """
    if currency != plans.SUBSCRIPTION_CURRENCY:
        return Reason.WRONG_CURRENCY
    if expected is not None and amount != expected:
        return Reason.WRONG_AMOUNT
    return None


@dataclass(frozen=True, slots=True)
class Classification:
    """Наш вердикт о пришедшем платеже."""

    kind: PaymentKind
    reason: Reason | None = None
    payload: InvoicePayload | None = None
    period_end: datetime | None = None
    # Срок посчитан нами, потому что Telegram его не прислал: это оценка, а не факт.
    estimated: bool = False
    # Telegram прислал дату, которой верить нельзя (ноль, прошлое, далёкое будущее):
    # срок тоже оценка, а владельцу — сообщение. «Нет даты» и «дата 0» — разные случаи.
    date_anomaly: bool = False


# Здравый смысл для даты окончания от Telegram: абсолютные границы, а не «около сейчас» —
# часы приложения тут ни при чём. Звёздных подписок до 2024 года не существует, так что
# нулевая дата (1970) и всё, что раньше, — не срок.
_EARLIEST = datetime(2024, 1, 1, tzinfo=UTC)
_LATEST = datetime(2100, 1, 1, tzinfo=UTC)


def classify_payment(
    facts: PaymentFacts,
    now: datetime,
    *,
    first: StoredPayment | None = None,
    first_unknown: bool = True,
) -> Classification:
    """Платёж → подписка (первый платёж или продление) либо «не наш», который вернётся сам.

    Срок берём у Telegram (`subscription_expiration_date`): продлевает подписку он, и его
    дата единственная правильная. Своя арифметика нужна только если даты нет, и тогда она
    помечена как оценка. Платёж, у которого нет ни одного признака подписки, подпиской не
    считается: счёт был выписан как подписочный, и разовая оплата по нему — сбой Telegram
    или подделка, а не покупка.

    Первый платёж подписки узнаётся ДВУМЯ признаками: флаг `is_first_recurring` и отсутствие
    записи о первом платеже в журнале (`first is None`). Каждый нужен: флаг теряется, если
    Telegram его не прислал, запись — если журнал недоступен был в первый раз. Первый платёж
    сверяется с текущей ценой, продление — с суммой первого платежа (см. `check_price`).
    `first_unknown` — журнал не спрашивали или он не ответил: продление принимается без
    сверки суммы (по умолчанию: вызывающий, который про журнал не знает, ничего не сверяет).
    """
    payload = parse_payload(facts.payload)
    if isinstance(payload, Reason):
        return Classification(PaymentKind.UNKNOWN, payload)
    is_first = (
        facts.is_first_recurring
        or (facts.expiration is not None and not facts.is_recurring)
        or (first is None and not first_unknown)
    )
    expected: int | None = plans.SUBSCRIPTION_STARS
    if not is_first:
        expected = None if first is None else first.amount
    refusal = check_amount(facts.currency, facts.total_amount, expected) or check_buyer(
        facts.payer_id, payload
    )
    if refusal is not None:
        return Classification(PaymentKind.UNKNOWN, refusal, payload)
    if not (facts.is_recurring or facts.is_first_recurring or facts.expiration is not None):
        return Classification(PaymentKind.UNKNOWN, Reason.NOT_A_SUBSCRIPTION, payload)
    kind = PaymentKind.FIRST if is_first else PaymentKind.RENEWAL
    period_end = _from_unix(facts.expiration)
    estimate = now + timedelta(seconds=plans.SUBSCRIPTION_PERIOD_S)
    if period_end is None:
        return Classification(kind, None, payload, estimate, estimated=True)
    if facts.expiration_estimated:
        return Classification(kind, None, payload, period_end, estimated=True)
    if not _EARLIEST <= period_end <= _LATEST:
        return Classification(kind, None, payload, estimate, estimated=True, date_anomaly=True)
    return Classification(kind, None, payload, period_end)


def _from_unix(stamp: int | None) -> datetime | None:
    """Дата от Telegram либо `None`, если её нет или она не читается (не роняем запись платежа).

    Ноль — это дата (1970 год), а не «нет даты»: `if not stamp` склеивал их, и платёж с
    нулевым сроком выглядел как платёж без срока.
    """
    if stamp is None:
        return None
    try:
        return datetime.fromtimestamp(stamp, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
