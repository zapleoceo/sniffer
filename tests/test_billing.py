"""Правила оплаты звёздами. Без сети и без базы — только то, что решается чистыми функциями.

Числа здесь не из памяти: период 2 592 000 и «одна позиция в `prices`» проверены живым
вызовом `createInvoiceLink` 01.09.2026 и справочником Bot API; цена 10 ⭐ — решение
владельца от 03.10.2026 (`docs/monetization.md`), а в код она попадает одной константой.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sniffer.bot.billing import (
    MAX_PAYLOAD_BYTES,
    CheckoutFacts,
    InvoicePayload,
    PaymentFacts,
    check_buyer,
    check_price,
    classify_payment,
    parse_payload,
    parse_refund_args,
)
from sniffer.domain import plans
from sniffer.domain.billing import PaymentKind, Reason

NOW = datetime.now(UTC).replace(microsecond=0)
NONCE = "0123456789ab"
PAYLOAD = InvoicePayload(42, "2026-10-03", NONCE)


# ── нагрузка счёта ──────────────────────────────────────────────────────────


def test_the_payload_survives_the_round_trip() -> None:
    assert PAYLOAD.encode() == "v2:s:42:2026-10-03:0123456789ab"
    assert parse_payload(PAYLOAD.encode()) == PAYLOAD


def test_the_payload_fits_the_telegram_limit_even_for_the_longest_user_id() -> None:
    longest = InvoicePayload(99_999_999_999_999_999_999, "2026-10-03", NONCE)

    assert len(longest.encode().encode()) <= MAX_PAYLOAD_BYTES == 128
    assert parse_payload(longest.encode()) == longest


@pytest.mark.parametrize(
    "junk",
    [
        "",
        "чужое",
        "v2:s:",
        "v2:s:abc:2026-10-03:0123456789ab",
        "v1:s:42:2026-10-03:0123456789ab",
        "v2:x:42:2026-10-03:0123456789ab",
        "v2:s:42:2026-10-03:0123456789ab:extra",
        "v2:s:42:2026-10-03:0123456789AB",
        "v2:s:42:2026-10-03:0123456789a",
        "v2:s:042:2026-10-03:0123456789ab",
        "v2:s:0:2026-10-03:0123456789ab",
        "v2:s:" + "9" * 21 + ":2026-10-03:0123456789ab",
        " v2:s:42:2026-10-03:0123456789ab",
    ],
)
def test_a_foreign_payload_is_refused_not_crashed(junk: str) -> None:
    """В `pre_checkout` падение означало бы неотвеченный запрос и сорванный платёж."""
    assert parse_payload(junk) is Reason.BAD_PAYLOAD


@pytest.mark.parametrize(
    "digits",
    ["²", "٤٢", "４２", "४２", "①", "4٢", "4²"],
    ids=[
        "superscript",
        "arabic-indic",
        "fullwidth",
        "devanagari",
        "circled",
        "ascii-then-arabic",
        "ascii-then-super",
    ],
)
def test_unicode_digits_are_not_digits(digits: str) -> None:
    """`str.isdigit()` истинно для «²» и арабо-индийских цифр, а `int()` на них падает ValueError.

    На счёте, подписанном самим Telegram, это недостижимо — но docstring старого разбора
    обещал «не падать», а проверка была `isdigit`. Теперь только ASCII `[0-9]`.
    """
    assert parse_payload(f"v2:s:{digits}:2026-10-03:{NONCE}") is Reason.BAD_PAYLOAD
    assert parse_payload(f"v2:s:42:2026-1{digits}-03:{NONCE}") is Reason.BAD_PAYLOAD
    assert parse_payload(f"sub:{digits}") is Reason.BAD_PAYLOAD


def test_a_trailing_newline_is_not_part_of_the_payload() -> None:
    """`$` в регулярке пропускает хвостовой перевод строки, `fullmatch` — нет."""
    assert parse_payload(PAYLOAD.encode() + "\n") is Reason.BAD_PAYLOAD


def test_the_old_per_topic_invoice_is_recognised_as_old_not_as_junk() -> None:
    """Счета-сообщения прежней модели остались в чатах с кнопкой Pay."""
    assert parse_payload("sub:42") is Reason.LEGACY_INVOICE
    assert parse_payload("sub:") is Reason.BAD_PAYLOAD


@pytest.mark.parametrize(
    ("version", "nonce"),
    [("2026-10-3", NONCE), ("2026-10-03", "ZZZZZZZZZZZZ"), ("2026-10-03", "short")],
)
def test_we_never_issue_a_payload_we_would_refuse_back(version: str, nonce: str) -> None:
    """Опечатка в версии или соли иначе тихо стала бы «счёт устарел» на первой оплате."""
    with pytest.raises(ValueError, match="не разбирается"):
        InvoicePayload(42, version, nonce)


# ── сумма, валюта, плательщик ───────────────────────────────────────────────


def test_the_price_check_follows_the_single_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    assert check_price("XTR", plans.SUBSCRIPTION_STARS) is None

    monkeypatch.setattr(plans, "SUBSCRIPTION_STARS", 77)

    assert check_price("XTR", 77) is None
    assert check_price("XTR", 10) is Reason.WRONG_AMOUNT, "старый счёт на прежнюю цену не проходит"


@pytest.mark.parametrize(
    ("currency", "amount", "expected"),
    [
        ("XTR", 1, Reason.WRONG_AMOUNT),
        ("XTR", 11, Reason.WRONG_AMOUNT),
        ("XTR", 0, Reason.WRONG_AMOUNT),
        ("XTR", -10, Reason.WRONG_AMOUNT),
        ("USD", 10, Reason.WRONG_CURRENCY),
        ("xtr", 10, Reason.WRONG_CURRENCY),
        ("", 10, Reason.WRONG_CURRENCY),
        ("USD", 1, Reason.WRONG_CURRENCY),
    ],
)
def test_a_wrong_price_or_currency_is_refused(currency: str, amount: int, expected: Reason) -> None:
    assert check_price(currency, amount) is expected


def test_only_the_addressee_may_pay_the_invoice() -> None:
    """Ссылку на счёт может открыть кто угодно; личность — из апдейта, а не из нагрузки."""
    assert check_buyer(42, PAYLOAD) is None
    assert check_buyer(43, PAYLOAD) is Reason.FOREIGN_BUYER
    assert check_buyer(None, PAYLOAD) is Reason.FOREIGN_BUYER


def test_the_checkout_facts_are_plain_data() -> None:
    facts = CheckoutFacts(buyer_id=42, currency="XTR", total_amount=10, payload="x")

    assert (facts.buyer_id, facts.currency, facts.total_amount) == (42, "XTR", 10)


# ── вердикт о платеже ───────────────────────────────────────────────────────

EXPIRATION = int((NOW + timedelta(days=30)).timestamp())


def facts(**overrides: object) -> PaymentFacts:
    fields: dict[str, object] = {
        "payer_id": 42,
        "currency": "XTR",
        "total_amount": 10,
        "payload": PAYLOAD.encode(),
        "charge_id": "charge-1",
        "expiration": EXPIRATION,
        "is_recurring": True,
        "is_first_recurring": True,
        "raw": {},
    }
    fields.update(overrides)
    return PaymentFacts(**fields)  # type: ignore[arg-type]


def test_the_first_payment_of_a_subscription_takes_its_term_from_telegram() -> None:
    """Продлевает подписку Telegram, и его дата единственная правильная."""
    verdict = classify_payment(facts(), NOW + timedelta(days=400))

    assert verdict.kind is PaymentKind.FIRST
    assert verdict.reason is None
    assert verdict.payload == PAYLOAD
    assert verdict.period_end == datetime.fromtimestamp(EXPIRATION, tz=UTC)
    assert verdict.estimated is False, "срок от Telegram — не оценка, и часы приложения ни при чём"


def test_a_renewal_has_no_first_flag() -> None:
    verdict = classify_payment(facts(is_first_recurring=False), NOW)

    assert verdict.kind is PaymentKind.RENEWAL


def test_the_first_flag_wins_over_the_recurring_one() -> None:
    assert classify_payment(facts(is_recurring=True, is_first_recurring=True), NOW).kind is (
        PaymentKind.FIRST
    )
    assert classify_payment(facts(is_recurring=False, is_first_recurring=True), NOW).kind is (
        PaymentKind.FIRST
    )


def test_a_term_without_any_flag_is_still_the_first_payment() -> None:
    """Документация не обещает, какие именно флаги стоят на первом платеже: срок — признак."""
    verdict = classify_payment(facts(is_recurring=False, is_first_recurring=False), NOW)

    assert verdict.kind is PaymentKind.FIRST


def test_a_payment_with_no_sign_of_a_subscription_is_not_a_purchase() -> None:
    """Счёт выписан подписочным; разовая оплата по нему — сбой Telegram или подделка."""
    verdict = classify_payment(
        facts(is_recurring=False, is_first_recurring=False, expiration=None), NOW
    )

    assert (verdict.kind, verdict.reason) == (PaymentKind.UNKNOWN, Reason.NOT_A_SUBSCRIPTION)


def test_without_a_date_the_term_is_an_estimate_and_says_so() -> None:
    verdict = classify_payment(facts(expiration=None), NOW)

    assert verdict.kind is PaymentKind.FIRST
    assert verdict.estimated is True
    assert verdict.period_end == NOW + timedelta(seconds=plans.SUBSCRIPTION_PERIOD_S)


@pytest.mark.parametrize("absurd", [10**20, -(10**20)])
def test_an_absurd_date_does_not_stop_the_payment_from_being_recorded(absurd: int) -> None:
    """Платёж уже снят: падение на дате оставило бы его без записи."""
    verdict = classify_payment(facts(expiration=absurd), NOW)

    assert verdict.kind is PaymentKind.FIRST
    assert verdict.estimated is True


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"total_amount": 1}, Reason.WRONG_AMOUNT),
        ({"total_amount": 11}, Reason.WRONG_AMOUNT),
        ({"currency": "USD"}, Reason.WRONG_CURRENCY),
        ({"payer_id": 43}, Reason.FOREIGN_BUYER),
        ({"payer_id": None}, Reason.FOREIGN_BUYER),
    ],
)
def test_a_payment_that_does_not_fit_our_invoice_is_unknown_and_keeps_its_invoice(
    overrides: dict[str, object], reason: Reason
) -> None:
    verdict = classify_payment(facts(**overrides), NOW)

    assert (verdict.kind, verdict.reason) == (PaymentKind.UNKNOWN, reason)
    assert verdict.payload == PAYLOAD, "счёт разобран: по нему возврат найдёт подписку"
    assert verdict.period_end is None


def test_an_invoice_we_do_not_recognise_is_unknown() -> None:
    legacy = classify_payment(facts(payload="sub:42"), NOW)
    junk = classify_payment(facts(payload="что-то чужое"), NOW)

    assert (legacy.kind, legacy.reason, legacy.payload) == (
        PaymentKind.UNKNOWN,
        Reason.LEGACY_INVOICE,
        None,
    )
    assert (junk.kind, junk.reason) == (PaymentKind.UNKNOWN, Reason.BAD_PAYLOAD)


# ── команда владельца ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("charge-1", ("charge-1", None)),
        ("  charge-1  ", ("charge-1", None)),
        ("charge-1 169510539", ("charge-1", 169510539)),
    ],
)
def test_the_refund_command_takes_a_charge_and_optionally_a_client(
    args: str, expected: tuple[str, int | None]
) -> None:
    assert parse_refund_args(args) == expected


@pytest.mark.parametrize(
    "args",
    [
        "",
        "   ",
        "a b c",
        "charge-1 abc",
        "charge-1 0",
        "charge-1 042",
        "charge-1 ²",
        "x" * 257,
        "charge-1 " + "9" * 21,
        "bad" + chr(0) + "charge",
    ],
)
def test_a_crooked_refund_command_is_not_guessed_at(args: str) -> None:
    """Возврат — деньги: кривую команду переспрашивают, а не додумывают."""
    assert parse_refund_args(args) is None
