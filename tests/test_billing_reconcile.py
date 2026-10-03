"""Сверка: платёж без записи, запись без платежа, «не наш» платёж без возврата (дыра A5).

Порты — подделки (`tests/billing_support.py`); история звёзд лежит в `RecordingApi.history`.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from sniffer.bot import billing_owner_wording as owner_words
from sniffer.bot.billing_payments import PaymentDesk
from sniffer.bot.billing_reconcile import Every, StarsReconciler
from sniffer.domain import plans
from sniffer.domain.billing import (
    FROM_RECONCILE,
    EventKind,
    PaymentKind,
    PaymentRecord,
    StarTransaction,
)
from tests.billing_support import CLIENT, OWNER, Failing, FakeLedger, FakeSlots, RecordingApi
from tests.test_billing import NOW, PAYLOAD

LONG_AGO = NOW - timedelta(hours=3)


def build(
    *, pages: int = 5, page_size: int = 100
) -> tuple[StarsReconciler, FakeLedger, RecordingApi, FakeSlots]:
    order: list[str] = []
    ledger, api, slots = FakeLedger(order), RecordingApi(order), FakeSlots(order)
    desk = PaymentDesk(
        ledger=ledger, api=api, slots=slots, owner_id=OWNER, reply_hours=48, clock=lambda: NOW
    )
    reconciler = StarsReconciler(
        ledger=ledger,
        api=api,
        slots=slots,
        desk=desk,
        owner_id=OWNER,
        clock=lambda: NOW,
        pages=pages,
        page_size=page_size,
    )
    return reconciler, ledger, api, slots


def charge(
    ident: str = "charge-1", *, amount: int = 10, payload: str | None = None, incoming: bool = True
) -> StarTransaction:
    return StarTransaction(
        charge_id=ident,
        amount=amount,
        date=LONG_AGO,
        incoming=incoming,
        user_id=CLIENT,
        invoice_payload=payload if payload is not None else PAYLOAD.encode(),
        subscription_period=plans.SUBSCRIPTION_PERIOD_S,
    )


def stored(
    ident: str = "charge-1", *, kind: PaymentKind = PaymentKind.FIRST, payload: str | None = None
) -> PaymentRecord:
    return PaymentRecord(
        charge_id=ident,
        tg_user_id=CLIENT,
        amount=10,
        currency="XTR",
        kind=kind,
        invoice_payload=payload if payload is not None else PAYLOAD.encode(),
        is_recurring=True,
        is_first_recurring=kind is PaymentKind.FIRST,
        period_end=NOW + timedelta(days=20),
        raw={},
    )


# ── платёж без записи ───────────────────────────────────────────────────────


async def test_a_payment_that_telegram_has_and_the_ledger_lacks_is_recorded_by_the_reconciler() -> (
    None
):
    reconciler, ledger, api, slots = build()
    api.history = [charge()]

    report = await reconciler.reconcile()

    record = ledger.payments["charge-1"]
    assert report.recovered == 1
    assert record.source == FROM_RECONCILE and record.kind is PaymentKind.FIRST
    assert record.period_end_estimated is True, "срок сверки — оценка, а не факт Telegram"
    assert record.period_end == LONG_AGO + timedelta(seconds=plans.SUBSCRIPTION_PERIOD_S)
    assert CLIENT in slots.syncs, "слот включается тем же путём, что и от апдейта"


async def test_the_client_is_answered_and_the_owner_hears_about_a_recovered_payment_once() -> None:
    reconciler, _ledger, api, _slots = build()
    api.history = [charge()]

    await reconciler.reconcile()
    first_run = list(api.texts)
    await reconciler.reconcile()

    chats = [chat for chat, _ in first_run]
    assert chats.count(CLIENT) == 1 and chats.count(OWNER) == 1
    assert api.texts == first_run, "второй проход ничего нового не говорит"


async def test_a_foreign_payment_found_by_the_reconciler_is_returned_not_activated() -> None:
    reconciler, ledger, api, _slots = build()
    api.history = [charge("odd", amount=1)]

    await reconciler.reconcile()

    assert ledger.payments["odd"].kind is PaymentKind.UNKNOWN
    assert api.refunds == [(CLIENT, "odd")]


async def test_a_payment_already_in_the_ledger_is_not_recorded_twice() -> None:
    reconciler, ledger, api, _slots = build()
    await ledger.record_payment(stored())
    api.history = [charge()]

    report = await reconciler.reconcile()

    assert report.recovered == 0 and len(ledger.payments) == 1


# ── запись без платежа ──────────────────────────────────────────────────────


async def test_a_ledger_payment_missing_from_telegram_history_is_reported_not_changed() -> None:
    reconciler, ledger, api, _slots = build()
    await ledger.record_payment(stored())
    ledger.created["charge-1"] = LONG_AGO
    api.history = []

    report = await reconciler.reconcile()

    assert report.gaps == 1
    assert api.texts == [
        (OWNER, owner_words.owner_gap_unpaid(tg_user_id=CLIENT, amount=10, charge_id="charge-1"))
    ]
    assert ledger.refunded == set() and ledger.refunding == set()


async def test_the_gap_is_reported_once_not_every_quarter_of_an_hour() -> None:
    reconciler, ledger, api, _slots = build()
    await ledger.record_payment(stored())

    await reconciler.reconcile()
    await reconciler.reconcile()

    assert len(api.texts) == 1
    assert any(e.kind is EventKind.RECONCILE_GAP for e in ledger.events)


async def test_a_fresh_payment_is_left_to_the_update_handler() -> None:
    """Обработчик апдейта мог ещё не закончить: сверка не лезет в платёж моложе десяти минут."""
    reconciler, ledger, api, _slots = build()
    await ledger.record_payment(stored())
    ledger.created["charge-1"] = NOW - timedelta(minutes=2)

    report = await reconciler.reconcile()

    assert report.gaps == 0 and api.texts == []


async def test_an_incomplete_history_does_not_blame_a_payment_older_than_what_it_saw() -> None:
    """Страниц просмотрено мало: старый платёж мог не поместиться, а не пропасть."""
    reconciler, ledger, api, _slots = build(pages=1, page_size=1)
    await ledger.record_payment(stored("ancient"))
    ledger.created["ancient"] = NOW - timedelta(days=1, hours=5)
    api.history = [charge("recent-1"), charge("recent-2")]

    report = await reconciler.reconcile()

    assert report.gaps == 0


# ── «не наш» платёж без возврата ────────────────────────────────────────────


async def test_a_foreign_payment_left_unrefunded_by_a_crash_is_returned_later() -> None:
    reconciler, ledger, api, _slots = build()
    await ledger.record_payment(stored("odd", kind=PaymentKind.UNKNOWN))
    api.history = [charge("odd", amount=1)]

    report = await reconciler.reconcile()

    assert report.settled == 1
    assert api.refunds == [(CLIENT, "odd")]
    assert "odd" in ledger.refunded
    assert any(chat == CLIENT for chat, _ in api.texts), "клиенту сказано, что звёзды вернулись"


async def test_a_refund_that_was_decided_but_not_finished_is_finished() -> None:
    reconciler, ledger, api, _slots = build()
    await ledger.record_payment(stored("half"))
    await ledger.mark_refunding("half")
    api.history = [charge("half")]

    report = await reconciler.reconcile()

    assert report.settled == 1 and "half" in ledger.refunded


async def test_a_refund_that_keeps_failing_is_reported_to_the_owner_once() -> None:
    reconciler, ledger, api, _slots = build()
    await ledger.record_payment(stored("odd", kind=PaymentKind.UNKNOWN))
    api.history = [charge("odd", amount=1)]
    api.failures["refund_star_payment"] = Failing("нет сети")

    first = await reconciler.reconcile()
    await reconciler.reconcile()

    assert first.settled == 0
    owner_texts = [text for chat, text in api.texts if chat == OWNER]
    assert len(owner_texts) == 1 and "/refund odd" in owner_texts[0]


async def test_a_young_unknown_payment_is_left_to_its_own_handler() -> None:
    reconciler, ledger, api, _slots = build()
    await ledger.record_payment(stored("odd", kind=PaymentKind.UNKNOWN))
    ledger.created["odd"] = NOW - timedelta(minutes=1)
    api.history = [charge("odd", amount=1)]

    await reconciler.reconcile()

    assert api.refunds == []


# ── возврат, о котором журнал не знал ───────────────────────────────────────


async def test_a_refund_unknown_to_the_ledger_is_marked_and_slots_are_recomputed() -> None:
    reconciler, ledger, api, slots = build()
    await ledger.record_payment(stored())
    api.history = [charge(incoming=False)]

    report = await reconciler.reconcile()

    assert report.marked_refunded == 1 and "charge-1" in ledger.refunded
    assert CLIENT in slots.syncs


# ── пересчёт слотов и устойчивость ──────────────────────────────────────────


async def test_slots_of_everyone_with_a_recent_payment_are_recomputed() -> None:
    reconciler, ledger, api, slots = build()
    await ledger.record_payment(stored())
    api.history = [charge()]

    report = await reconciler.reconcile()

    assert slots.syncs == [CLIENT] and report.resynced == 1


async def test_a_broken_history_stops_nothing_else_from_being_safe() -> None:
    reconciler, ledger, api, _slots = build()
    await ledger.record_payment(stored("odd", kind=PaymentKind.UNKNOWN))
    api.failures["star_transactions"] = Failing("нет сети")

    report = await reconciler.reconcile()

    assert report.history_failed is True and api.refunds == []


@pytest.mark.parametrize(
    "method", ["get_payment", "payments_since", "unsettled_refunds", "has_event", "record_event"]
)
async def test_an_unknown_failure_at_any_ledger_step_never_escapes(method: str) -> None:
    reconciler, ledger, api, _slots = build()
    await ledger.record_payment(stored("odd", kind=PaymentKind.UNKNOWN))
    api.history = [charge("odd", amount=1), charge("other")]
    ledger.failures[method] = Failing("база")

    await reconciler.reconcile()


async def test_an_interrupt_is_raised_after_the_pass_not_swallowed() -> None:
    reconciler, _ledger, api, _slots = build()
    api.failures["star_transactions"] = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        await reconciler.reconcile()


# ── ритм ────────────────────────────────────────────────────────────────────


async def test_the_pass_runs_at_once_and_then_not_more_often_than_the_interval() -> None:
    ticks = iter([0.0, 10.0, 899.0, 901.0])
    every = Every(timedelta(minutes=15), clock=lambda: next(ticks))
    calls: list[int] = []

    async def work() -> int:
        calls.append(1)
        return 1

    results = [await every.run(work) for _ in range(4)]

    assert results == [1, 0, 0, 1] and len(calls) == 2
