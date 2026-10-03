"""Платёж и слот: ответ клиенту строится из итога пересчёта, возврат пишется ДО вызова Telegram.

Находки ревью A5, которые здесь закрыты тестами: продление сверяется с суммой ПЕРВОГО платежа
подписки, первый платёж узнаётся двумя признаками, дата «0» — не «нет даты», свой возврат не
принимается за чужой, оплативший не получает «подписка действует» без слота.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sniffer.bot import billing_owner_wording as owner_words
from sniffer.bot import billing_wording as words
from sniffer.bot.billing import RefundedFacts, classify_payment
from sniffer.bot.billing_payments import PaymentDesk
from sniffer.domain import plans
from sniffer.domain.billing import PAID, PaymentKind, Reason, StoredPayment
from sniffer.domain.slots import SlotState
from tests.billing_support import CLIENT, OWNER, Failing, FakeLedger, FakeSlots, RecordingApi
from tests.test_billing import EXPIRATION, NOW, PAYLOAD, facts


def build(
    *, owner: int = OWNER
) -> tuple[PaymentDesk, FakeLedger, RecordingApi, FakeSlots, list[str]]:
    order: list[str] = []
    ledger, api, slots = FakeLedger(order), RecordingApi(order), FakeSlots(order)
    desk = PaymentDesk(
        ledger=ledger, api=api, slots=slots, owner_id=owner, reply_hours=48, clock=lambda: NOW
    )
    return desk, ledger, api, slots, order


def until() -> datetime:
    return datetime.fromtimestamp(EXPIRATION, tz=UTC)


# ── слот включается пересчётом, ответ — из его итога ───────────────────────


async def test_a_first_payment_recomputes_the_slots_of_the_payer_and_names_the_free_one() -> None:
    desk, ledger, _api, slots, _order = build()
    slots.state = SlotState(slots=1, holding=0)

    reply = await desk.on_payment(facts())

    assert slots.syncs == [CLIENT]
    assert reply == words.thanks_first(until(), 1)
    assert "/requests" in (reply or "") and "Следить" in (reply or "")
    assert "charge-1" in ledger.payments


async def test_when_every_slot_is_taken_the_thanks_does_not_promise_a_free_one() -> None:
    desk, _ledger, _api, slots, _order = build()
    slots.state = SlotState(slots=1, holding=1)

    reply = await desk.on_payment(facts())

    assert reply == words.thanks_first(until(), 0)
    assert reply != words.thanks_first(until(), 1)


async def test_a_renewal_says_how_many_paused_trackings_came_back() -> None:
    desk, _ledger, _api, slots, _order = build()
    await desk.on_payment(facts())
    slots.state = SlotState(slots=1, holding=1, resumed=1)

    reply = await desk.on_payment(facts(is_first_recurring=False, charge_id="charge-2"))

    assert reply == words.thanks_renewal(until(), 1)
    assert "возобновились" in (reply or "")


async def test_a_renewal_with_nothing_to_resume_stays_quiet_about_it() -> None:
    desk, _ledger, _api, _slots, _order = build()
    await desk.on_payment(facts())

    reply = await desk.on_payment(facts(is_first_recurring=False, charge_id="charge-2"))

    assert reply == words.thanks_renewal(until(), 0)
    assert "возобновились" not in (reply or "")


async def test_if_the_slot_cannot_be_enabled_the_payer_is_not_told_the_subscription_works() -> None:
    """«Подписка действует» без слота — обещание, которое ничем не подкреплено."""
    desk, ledger, api, slots, _order = build()
    slots.failure = Failing("база")

    reply = await desk.on_payment(facts())

    assert reply == words.payment_slot_pending(until(), 48)
    assert "Следить" not in (reply or "")
    assert "charge-1" in ledger.payments, "платёж записан, слот — дело сверки"
    assert api.texts == [
        (OWNER, owner_words.owner_slots_unsynced(tg_user_id=CLIENT, charge_id="charge-1"))
    ]


async def test_a_rejected_payment_does_not_open_a_slot() -> None:
    desk, _ledger, _api, slots, _order = build()

    await desk.on_payment(facts(total_amount=1))

    assert slots.syncs == [CLIENT], "после возврата пересчёт нужен — но только он, не включение"


async def test_a_redelivered_payment_does_not_recompute_again() -> None:
    desk, _ledger, _api, slots, _order = build()

    await desk.on_payment(facts())
    await desk.on_payment(facts())

    assert slots.syncs == [CLIENT]


# ── возврат: сначала запись, потом Telegram ────────────────────────────────


async def test_the_refund_is_written_to_the_ledger_before_telegram_is_called() -> None:
    """Иначе `refunded_payment` обгонит нашу отметку и покажется возвратом «не нашими руками»."""
    desk, _ledger, _api, _slots, order = build()

    await desk.on_payment(facts(total_amount=1))

    wanted = ["ledger:mark_refunding", "api:refund_star_payment", "ledger:mark_refunded"]
    positions = [order.index(item) for item in wanted]
    assert positions == sorted(positions), order


async def test_a_refund_frees_the_slot_at_once() -> None:
    desk, _ledger, _api, slots, order = build()
    await desk.on_payment(facts())
    slots.syncs.clear()

    await desk.refund("charge-1", user_id=None)

    assert slots.syncs == [CLIENT]
    assert order.index("api:refund_star_payment") < len(order) - 1


async def test_an_unreachable_ledger_does_not_stop_a_refund() -> None:
    desk, _ledger, api, _slots, _order = build()
    await desk.on_payment(facts())
    _ledger.failures["mark_refunding"] = Failing("база")

    await desk.refund("charge-1", user_id=None)

    assert api.refunds == [(CLIENT, "charge-1")], "деньги клиента важнее учёта"


async def test_our_own_refund_message_is_not_taken_for_a_foreign_one() -> None:
    """Сообщение о возврате обогнало нашу отметку: в журнале уже `refunding`."""
    desk, ledger, api, _slots, _order = build()
    await desk.on_payment(facts())
    await ledger.mark_refunding("charge-1")
    api.texts.clear()

    await desk.on_refunded(RefundedFacts(CLIENT, "charge-1", 10, PAYLOAD.encode(), update_id=7))

    assert api.texts == []


async def test_a_refund_made_outside_the_bot_tells_the_owner() -> None:
    desk, _ledger, api, _slots, _order = build()
    await desk.on_payment(facts())
    api.texts.clear()

    await desk.on_refunded(RefundedFacts(CLIENT, "charge-1", 10, PAYLOAD.encode(), update_id=7))

    assert [chat for chat, _ in api.texts] == [OWNER]


async def test_a_refund_of_an_unknown_payment_tells_the_owner() -> None:
    desk, _ledger, api, _slots, _order = build()

    await desk.on_refunded(RefundedFacts(CLIENT, "ghost", 10, PAYLOAD.encode(), update_id=8))

    assert [chat for chat, _ in api.texts] == [OWNER]


async def test_a_refund_message_recomputes_the_slots_too() -> None:
    desk, _ledger, _api, slots, _order = build()
    await desk.on_payment(facts())
    slots.syncs.clear()

    await desk.on_refunded(RefundedFacts(CLIENT, "charge-1", 10, PAYLOAD.encode(), update_id=9))

    assert slots.syncs == [CLIENT]


# ── сумма продления — от первого платежа, а не от текущей цены ─────────────


async def test_a_renewal_of_an_old_price_is_accepted_after_the_tariff_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk, ledger, api, _slots, _order = build()
    await desk.on_payment(facts(total_amount=plans.SUBSCRIPTION_STARS))
    monkeypatch.setattr(plans, "SUBSCRIPTION_STARS", plans.SUBSCRIPTION_STARS + 5)

    reply = await desk.on_payment(
        facts(is_first_recurring=False, charge_id="charge-2", total_amount=10)
    )

    assert ledger.payments["charge-2"].kind is PaymentKind.RENEWAL
    assert api.refunds == [], "честное продление старой цены не «чужой» платёж"
    assert reply == words.thanks_renewal(until(), 0)


async def test_a_renewal_that_differs_from_the_first_payment_is_returned() -> None:
    desk, ledger, api, _slots, _order = build()
    await desk.on_payment(facts(total_amount=10))

    await desk.on_payment(facts(is_first_recurring=False, charge_id="charge-2", total_amount=3))

    assert ledger.payments["charge-2"].kind is PaymentKind.UNKNOWN
    assert api.refunds == [(CLIENT, "charge-2")]


async def test_when_the_ledger_cannot_name_the_first_payment_a_renewal_is_not_returned() -> None:
    """Сверять нечем: честный платёж не возвращаем, валюту проверяем по-прежнему."""
    desk, ledger, api, _slots, _order = build()
    ledger.failures["first_payment_of"] = Failing("база")

    await desk.on_payment(facts(is_first_recurring=False, charge_id="charge-2", total_amount=3))

    assert ledger.payments["charge-2"].kind is PaymentKind.RENEWAL
    assert api.refunds == []


def test_the_first_payment_is_recognised_by_the_flag() -> None:
    """Признак 1: флаг. Запись о первом платеже есть, но этот платёж помечен первым."""
    stored = _stored(amount=7)

    verdict = classify_payment(
        facts(is_first_recurring=True, total_amount=7), NOW, first=stored, first_unknown=False
    )

    assert verdict.reason is Reason.WRONG_AMOUNT


def test_the_first_payment_is_recognised_by_the_absence_of_a_record() -> None:
    """Признак 2: записи нет — это первый платёж, даже без флага; цена текущая."""
    flagless = facts(is_first_recurring=False, total_amount=7)

    assert (
        classify_payment(flagless, NOW, first=None, first_unknown=False).reason
        is Reason.WRONG_AMOUNT
    )
    assert (
        classify_payment(flagless, NOW, first=_stored(amount=7), first_unknown=False).reason is None
    )


def test_neither_sign_means_a_renewal_and_the_first_amount_decides() -> None:
    renewal = facts(is_first_recurring=False, total_amount=7)

    assert (
        classify_payment(renewal, NOW, first=_stored(amount=7), first_unknown=False).kind
        is PaymentKind.RENEWAL
    )
    assert (
        classify_payment(renewal, NOW, first=_stored(amount=8), first_unknown=False).reason
        is Reason.WRONG_AMOUNT
    )


# ── дата окончания: «нет» и «ноль» — разные вещи ───────────────────────────


def test_a_missing_date_is_an_estimate_without_alarm() -> None:
    verdict = classify_payment(facts(expiration=None, is_first_recurring=True), NOW)

    assert verdict.estimated is True and verdict.date_anomaly is False
    assert verdict.period_end == NOW + timedelta(seconds=plans.SUBSCRIPTION_PERIOD_S)


def test_a_zero_date_is_a_present_but_absurd_date_not_a_missing_one() -> None:
    verdict = classify_payment(
        facts(expiration=0, is_recurring=False, is_first_recurring=False), NOW
    )

    assert verdict.kind is PaymentKind.FIRST, (
        "дата есть (пусть нулевая): признак подписки не теряется"
    )
    assert verdict.date_anomaly is True and verdict.estimated is True
    assert verdict.period_end == NOW + timedelta(seconds=plans.SUBSCRIPTION_PERIOD_S)


async def test_an_absurd_date_is_reported_to_the_owner_and_the_payment_is_still_recorded() -> None:
    desk, ledger, api, _slots, _order = build()

    await desk.on_payment(facts(expiration=0))

    assert ledger.payments["charge-1"].period_end_estimated is True
    assert [chat for chat, _ in api.texts] == [OWNER]


async def test_a_missing_date_is_recorded_as_an_estimate_without_bothering_the_owner() -> None:
    desk, ledger, api, _slots, _order = build()

    await desk.on_payment(facts(expiration=None))

    assert ledger.payments["charge-1"].period_end_estimated is True
    assert api.texts == []


def _stored(*, amount: int) -> StoredPayment:
    """Строка журнала с первым платежом подписки на заданную сумму."""
    return StoredPayment(
        charge_id="first",
        tg_user_id=CLIENT,
        amount=amount,
        currency="XTR",
        kind=PaymentKind.FIRST.value,
        status=PAID,
        invoice_payload=PAYLOAD.encode(),
        is_recurring=True,
        is_first_recurring=True,
        period_end=None,
        refunded_at=None,
        created_at=NOW,
    )
