"""Разбор оплаты звёздами. Без сети и без базы — только правила Telegram.

Числа здесь не из памяти: 1 звезда и период 2592000 проверены живым вызовом
`createInvoiceLink` 01.09.2026 (ноль звёзд и период в сутки Telegram отверг).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sniffer.bot.billing import (
    SUBSCRIPTION_CURRENCY,
    SUBSCRIPTION_PERIOD_S,
    SUBSCRIPTION_STARS,
    passport_root_from,
    payload_for,
    purchase_from,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def test_the_only_period_telegram_accepts_is_thirty_days() -> None:
    """Не наш выбор: 86400 отвергается с SUBSCRIPTION_PERIOD_INVALID."""
    assert SUBSCRIPTION_PERIOD_S == 2_592_000


def test_the_price_is_one_star_in_the_stars_currency() -> None:
    assert (SUBSCRIPTION_STARS, SUBSCRIPTION_CURRENCY) == (1, "XTR")


def test_the_payload_survives_the_round_trip() -> None:
    assert passport_root_from(payload_for(42)) == 42


def test_a_foreign_payload_is_refused_not_crashed() -> None:
    """В `pre_checkout` падение означало бы неотвеченный запрос и срыв платежа."""
    for junk in ("", "чужое", "sub:", "sub:abc", "other:42", "sub:42:extra"):
        assert passport_root_from(junk) is None


def test_the_expiry_comes_from_telegram_not_from_our_clock() -> None:
    """Продлевает подписку Telegram — его дата единственная правильная.

    Своя арифметика разошлась бы с ней на любой задержке платежа, и подписка
    выключалась бы раньше оплаченного.
    """
    stamp = int(datetime(2026, 10, 1, 12, 0, tzinfo=UTC).timestamp())
    purchase = purchase_from(
        user_id=7,
        payload=payload_for(42),
        charge_id="charge-1",
        amount=1,
        expiration=stamp,
        is_recurring=False,
        now=NOW,
    )

    assert purchase is not None
    assert purchase.until == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def test_without_a_date_we_fall_back_to_the_period_we_sold() -> None:
    """Дату присылают не всегда; продали месяц — считаем месяц."""
    purchase = purchase_from(
        user_id=7,
        payload=payload_for(42),
        charge_id="charge-1",
        amount=1,
        expiration=None,
        is_recurring=False,
        now=NOW,
    )

    assert purchase is not None
    assert (purchase.until - NOW).total_seconds() == SUBSCRIPTION_PERIOD_S


def test_a_payment_for_someone_elses_invoice_is_not_a_purchase() -> None:
    assert (
        purchase_from(
            user_id=7,
            payload="чужой счёт",
            charge_id="charge-1",
            amount=1,
            expiration=None,
            is_recurring=False,
            now=NOW,
        )
        is None
    )


def test_the_charge_id_becomes_the_idempotency_key() -> None:
    """`external_id` — единственное, что отличает повтор апдейта от новой оплаты."""
    purchase = purchase_from(
        user_id=7,
        payload=payload_for(42),
        charge_id="charge-xyz",
        amount=1,
        expiration=None,
        is_recurring=True,
        now=NOW,
    )

    assert purchase is not None
    assert purchase.payment.external_id == "charge-xyz"
    assert purchase.payment.is_recurring is True
    assert purchase.payment.amount == 1
