"""Платёж после списания: журнал первым делом, ответ, автоматический возврат «не нашего».

Звёзды уже сняты, и любая наша проблема решается на нашей стороне. Порты — подделки
(`tests/billing_support.py`): по вызовам видно, что записано, что возвращено и кому сказано.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest

from sniffer.bot import billing_wording as words
from sniffer.bot.billing import RefundedFacts
from sniffer.bot.billing_payments import PaymentDesk, already_refunded
from sniffer.bot.billing_ports import BotApiError
from sniffer.domain.billing import EventKind, PaymentKind, Reason
from tests.billing_support import CLIENT, OWNER, Failing, FakeLedger, FakeSlots, RecordingApi
from tests.test_billing import EXPIRATION, NOW, facts

OTHER_PAYLOAD = "v2:s:42:2026-10-03:ffffffffffff"


def desk_with_slots(
    *, owner: int = OWNER
) -> tuple[PaymentDesk, FakeLedger, RecordingApi, FakeSlots]:
    order: list[str] = []
    ledger, api = FakeLedger(order), RecordingApi(order)
    slots = FakeSlots(order)
    payments = PaymentDesk(
        ledger=ledger, api=api, slots=slots, owner_id=owner, reply_hours=48, clock=lambda: NOW
    )
    return payments, ledger, api, slots


def desk(*, owner: int = OWNER) -> tuple[PaymentDesk, FakeLedger, RecordingApi]:
    payments, ledger, api, _slots = desk_with_slots(owner=owner)
    return payments, ledger, api


def moment() -> datetime:
    return datetime.fromtimestamp(EXPIRATION, tz=UTC)


# ── свой платёж ─────────────────────────────────────────────────────────────


async def test_the_first_payment_is_recorded_whole_and_thanked() -> None:
    payments, ledger, api = desk()
    paid = facts(raw={"telegram_payment_charge_id": "charge-1", "total_amount": 10})

    reply = await payments.on_payment(paid)

    assert reply == words.thanks_first(moment(), 1)
    record = ledger.payments["charge-1"]
    assert record.kind is PaymentKind.FIRST and record.tg_user_id == CLIENT
    assert record.raw == paid.raw, "SuccessfulPayment как пришёл: по нему платёж разбирают руками"
    assert record.period_end == moment(), "срок от Telegram"
    assert api.refunds == [] and api.texts == [], (
        "своему платежу нечего возвращать и не о чем тревожить"
    )


async def test_a_renewal_is_answered_differently_from_the_first_payment() -> None:
    """Тот же ответ на продление выглядел бы как новое списание."""
    payments, ledger, _api = desk()
    await payments.on_payment(facts())

    reply = await payments.on_payment(facts(is_first_recurring=False, charge_id="charge-2"))

    assert reply == words.thanks_renewal(moment(), 0)
    assert reply != words.thanks_first(moment(), 1)
    assert ledger.payments["charge-2"].kind is PaymentKind.RENEWAL


async def test_a_redelivered_update_is_silent_and_changes_nothing() -> None:
    """Telegram повторяет апдейт, если бот не ответил вовремя; второе «спасибо» — как списание."""
    payments, ledger, api = desk()

    first = await payments.on_payment(facts())
    again = await payments.on_payment(facts())

    assert first is not None and again is None
    assert len(ledger.payments) == 1 and api.refunds == []


async def test_refund_status_write_failure_never_confirms_slot_sync() -> None:
    payments, ledger, api, slots = desk_with_slots()
    ledger.failures["mark_refunded"] = Failing("temporary database error")

    await payments.on_payment(facts(total_amount=1))

    assert api.refunds == [(CLIENT, "charge-1")]
    assert slots.syncs == []
    assert not await ledger.has_event(EventKind.REFUND_SYNCED, "charge-1")


# ── чужой платёж возвращается сам ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"total_amount": 1}, Reason.WRONG_AMOUNT),
        ({"total_amount": 11}, Reason.WRONG_AMOUNT),
        ({"currency": "USD"}, Reason.WRONG_CURRENCY),
        ({"payer_id": 777}, Reason.FOREIGN_BUYER),
        ({"payload": "sub:42"}, Reason.LEGACY_INVOICE),
        ({"payload": "что-то чужое"}, Reason.BAD_PAYLOAD),
        (
            {"is_recurring": False, "is_first_recurring": False, "expiration": None},
            Reason.NOT_A_SUBSCRIPTION,
        ),
    ],
)
async def test_a_payment_that_is_not_ours_is_recorded_refunded_and_explained(
    overrides: dict[str, object], reason: Reason
) -> None:
    payments, ledger, api = desk()
    paid = facts(**overrides)
    payer = paid.payer_id or CLIENT

    reply = await payments.on_payment(paid)

    assert reply == words.payment_refunded(reason)
    assert ledger.payments["charge-1"].kind is PaymentKind.UNKNOWN, "в журнале он есть"
    assert api.refunds == [(payer, "charge-1")] and "charge-1" in ledger.refunded
    (chat, alert) = api.texts[0]
    assert chat == OWNER and "charge-1" in alert and words.why(reason) in alert


async def test_the_payment_is_in_the_ledger_before_anything_is_done_with_it() -> None:
    """Журнал вперёд: упади всё остальное — платёж уже лежит и по нему можно вернуть."""
    payments, ledger, api = desk()

    await payments.on_payment(facts(total_amount=1))

    assert ledger.order.index("ledger:record_payment") < api.order.index("api:refund_star_payment")


async def test_a_failed_refund_is_told_to_the_client_and_to_the_owner() -> None:
    payments, ledger, api = desk()
    api.failures["refund_star_payment"] = BotApiError("Bad Request: USER_ID_INVALID")

    reply = await payments.on_payment(facts(total_amount=1))

    assert reply == words.payment_refund_failed(48)
    assert "charge-1" not in ledger.refunded, "в журнале не помечено то, чего не было"
    assert "не удался" in api.texts[0][1] and "USER_ID_INVALID" in api.texts[0][1]


@pytest.mark.parametrize(
    "text",
    [
        "Bad Request: CHARGE_ALREADY_REFUNDED",
        "charge already refunded",
        "Bad Request: charge_already_refunded",
    ],
)
async def test_a_second_refund_of_the_same_payment_is_a_success(text: str) -> None:
    payments, ledger, api = desk()
    api.failures["refund_star_payment"] = BotApiError(text)

    reply = await payments.on_payment(facts(total_amount=1))

    assert reply == words.payment_refunded(Reason.WRONG_AMOUNT)
    assert "charge-1" in ledger.refunded


def test_only_the_already_refunded_error_counts_as_done() -> None:
    assert already_refunded(BotApiError("Bad Request: CHARGE_ALREADY_REFUNDED"))
    assert not already_refunded(BotApiError("Bad Request: CHARGE_ID_EMPTY"))
    assert not already_refunded(Failing("CHARGE_ALREADY_REFUNDED"))
    assert not already_refunded(None)


async def test_a_recurring_payment_that_is_refunded_has_its_renewal_stopped() -> None:
    """Возврат первого платежа без отмены продления списал бы клиента снова через месяц."""
    payments, _ledger, api = desk()

    await payments.on_payment(facts(total_amount=1))

    assert api.cancels == [(CLIENT, "charge-1")]


async def test_a_one_off_payment_has_no_renewal_to_stop() -> None:
    payments, _ledger, api = desk()

    await payments.on_payment(
        facts(payload="что-то чужое", is_recurring=False, is_first_recurring=False)
    )

    assert api.refunds and api.cancels == []


async def test_a_renewal_that_could_not_be_stopped_is_told_to_the_owner() -> None:
    payments, _ledger, api = desk()
    api.failures["cancel_star_subscription"] = BotApiError("Bad Request")

    reply = await payments.on_payment(facts(total_amount=1))

    assert reply == words.payment_refunded(Reason.WRONG_AMOUNT), "клиенту возврат состоялся"
    assert "продление не отключено" in api.texts[0][1]


# ── когда не вышло записать ─────────────────────────────────────────────────


async def test_a_payment_that_could_not_be_recorded_reaches_the_owner_with_everything() -> None:
    """Звёзды сняты, база недоступна: платёж не теряется молча, а сообщается со всеми полями."""
    payments, ledger, api = desk()
    ledger.failures["record_payment"] = Failing("база недоступна")

    reply = await payments.on_payment(facts())

    assert reply == words.payment_unrecorded(48)
    (chat, alert) = api.texts[0]
    assert chat == OWNER
    assert "charge-1" in alert and "v2:s:42" in alert and "/refund charge-1" in alert
    assert api.refunds == [], "платёж валидный: возвращать его нельзя, пока не разобрались"


async def test_the_payer_comes_from_the_invoice_when_the_update_has_none() -> None:
    payments, ledger, _api = desk()

    await payments.on_payment(facts(payer_id=None))

    assert ledger.payments["charge-1"].tg_user_id == CLIENT


async def test_a_payment_with_no_payer_and_no_invoice_is_not_recordable_but_not_silent() -> None:
    payments, ledger, api = desk()

    reply = await payments.on_payment(facts(payer_id=None, payload="чужое"))

    assert reply == words.payment_unrecorded(48)
    assert ledger.payments == {} and "charge-1" in api.texts[0][1]


async def test_without_an_owner_the_refund_still_happens() -> None:
    """Некому писать — но возврат от этого не отменяется, а тревога уходит хотя бы в лог."""
    payments, ledger, api = desk(owner=0)

    reply = await payments.on_payment(facts(total_amount=1))

    assert reply == words.payment_refunded(Reason.WRONG_AMOUNT)
    assert api.texts == [] and "charge-1" in ledger.refunded


# ── команда владельца `/refund` ─────────────────────────────────────────────


async def paid_subscription(payments: PaymentDesk) -> None:
    assert await payments.on_payment(facts()) is not None


async def test_the_owner_refunds_a_recorded_payment_and_the_client_is_told() -> None:
    payments, ledger, api = desk()
    await paid_subscription(payments)

    reply = await payments.refund("charge-1", user_id=None)

    assert "Возврат выполнен" in reply and "charge-1" in ledger.refunded
    assert api.refunds == [(CLIENT, "charge-1")]
    assert (CLIENT, words.subscription_refunded()) in api.texts
    assert api.cancels == [(CLIENT, "charge-1")], "иначе подписка продлилась бы и списала снова"


async def test_a_payment_missing_from_the_ledger_needs_the_clients_id() -> None:
    payments, _ledger, api = desk()

    reply = await payments.refund("ghost", user_id=None)
    assert "нет в журнале" in reply and api.refunds == []

    reply = await payments.refund("ghost", user_id=555)
    assert "Возврат выполнен" in reply and api.refunds == [(555, "ghost")]


async def test_a_refund_that_telegram_rejected_is_reported_and_not_marked() -> None:
    payments, ledger, api = desk()
    await paid_subscription(payments)
    api.failures["refund_star_payment"] = BotApiError("Bad Request: CHARGE_ID_EMPTY")

    reply = await payments.refund("charge-1", user_id=None)

    assert "не прошёл" in reply and "CHARGE_ID_EMPTY" in reply
    assert ledger.refunded == set()


async def test_repeating_a_refund_is_harmless() -> None:
    payments, _ledger, api = desk()
    await paid_subscription(payments)
    await payments.refund("charge-1", user_id=None)
    api.failures["refund_star_payment"] = BotApiError("Bad Request: CHARGE_ALREADY_REFUNDED")

    reply = await payments.refund("charge-1", user_id=None)

    assert "Возврат выполнен" in reply


async def test_repeating_a_refund_does_not_tell_the_client_twice() -> None:
    payments, _ledger, api = desk()
    await paid_subscription(payments)
    await payments.refund("charge-1", user_id=None)
    api.texts.clear()

    await payments.refund("charge-1", user_id=None)

    assert (CLIENT, words.subscription_refunded()) not in api.texts


def test_only_the_configured_owner_is_the_owner() -> None:
    payments, _ledger, _api = desk()
    nobody, _l, _a = desk(owner=0)

    assert payments.is_owner(OWNER) and not payments.is_owner(CLIENT)
    assert not nobody.is_owner(0), "владелец не задан — ноль не владелец"


# ── сообщения Telegram о возврате и подписке ────────────────────────────────


def refunded(charge: str = "charge-1", **overrides: object) -> RefundedFacts:
    fields: dict[str, object] = {
        "payer_id": CLIENT,
        "charge_id": charge,
        "total_amount": 10,
        "payload": facts().payload,
        "update_id": 41,
    }
    fields.update(overrides)
    return RefundedFacts(**fields)  # type: ignore[arg-type]


async def test_our_own_refund_is_silent_when_telegram_confirms_it() -> None:
    payments, _ledger, api = desk()
    await paid_subscription(payments)
    await payments.refund("charge-1", user_id=None)
    api.texts.clear()

    await payments.on_refunded(refunded())

    assert api.texts == []


async def test_a_refund_made_elsewhere_is_marked_and_told_to_the_owner() -> None:
    """Поддержка Telegram, спор: журнал не знал, и владелец узнаёт."""
    payments, ledger, api = desk()
    await paid_subscription(payments)

    await payments.on_refunded(refunded())

    assert "charge-1" in ledger.refunded
    assert OWNER in [chat for chat, _text in api.texts] and "не через бота" in api.texts[0][1]
    assert [event.kind for event in ledger.events] == [
        EventKind.REFUNDED,
        EventKind.REFUND_SYNCED,
        EventKind.RENEWAL_CANCELED,
    ]


async def test_a_refund_of_a_payment_we_never_recorded_is_told_to_the_owner() -> None:
    payments, _ledger, api = desk()

    await payments.on_refunded(refunded("never-seen"))

    assert "never-seen" in api.texts[0][1]


async def test_subscription_changes_are_only_journaled() -> None:
    """Состояние слота эти события не меняют: доступ считается по срокам из платежей."""
    payments, ledger, api = desk()
    payload = facts().payload

    for number, state in enumerate(["canceled", "active", "failed", "что-то новое"], start=1):
        await payments.on_subscription(
            update_id=number, tg_user_id=CLIENT, payload=payload, state=state
        )

    assert [event.kind for event in ledger.events] == [
        EventKind.SUB_CANCELED,
        EventKind.SUB_ACTIVE,
        EventKind.SUB_FAILED,
        EventKind.SUB_OTHER,
    ]
    assert ledger.events[0].payload == {"invoice_payload": payload, "state": "canceled"}
    assert api.texts == [] and ledger.payments == {}


async def test_the_same_subscription_update_twice_is_one_event() -> None:
    payments, ledger, _api = desk()

    for _ in range(2):
        await payments.on_subscription(
            update_id=9, tg_user_id=CLIENT, payload="x", state="canceled"
        )

    assert len(ledger.events) == 1


# ── полнота охраны: чужой тип и прерывание на КАЖДОМ шаге каждого сценария ───

LEDGER_STEPS = {
    "record_payment",
    "get_payment",
    "mark_refunding",
    "mark_refunded",
    "first_payment_of",
    "first_charge_of",
    "record_event",
}
INTERRUPTS = [
    pytest.param(KeyboardInterrupt, id="KeyboardInterrupt"),
    pytest.param(asyncio.CancelledError, id="CancelledError"),
    pytest.param(lambda: BaseExceptionGroup("g", [KeyboardInterrupt()]), id="group"),
]


async def reject_flow(payments: PaymentDesk) -> object:
    # Другой charge id, чем у платежа из подготовки: тот же вернулся бы молчаливым повтором.
    return await payments.on_payment(facts(total_amount=1, charge_id="charge-2"))


async def accept_flow(payments: PaymentDesk) -> object:
    return await payments.on_payment(facts(charge_id="charge-3"))


async def refund_flow(payments: PaymentDesk) -> object:
    return await payments.refund("charge-1", user_id=None)


async def refunded_flow(payments: PaymentDesk) -> object:
    await payments.on_refunded(refunded())
    return None


async def subscription_flow(payments: PaymentDesk) -> object:
    await payments.on_subscription(update_id=1, tg_user_id=CLIENT, payload="x", state="failed")
    return None


# сценарий → шаги, через которые он ходит в порты. Список связан с кодом механически:
# `test_every_port_call_of_the_desk_is_in_the_matrix` сверяет его с исходником класса.
FLOWS: dict[str, tuple[Callable[[PaymentDesk], Awaitable[object]], tuple[str, ...]]] = {
    "on_payment_accepted": (accept_flow, ("first_payment_of", "record_payment", "sync")),
    "on_payment": (
        reject_flow,
        (
            "first_payment_of",
            "record_payment",
            "mark_refunding",
            "refund_star_payment",
            "mark_refunded",
            "sync",
            "first_charge_of",
            "cancel_star_subscription",
            "send_text",
        ),
    ),
    "refund": (
        refund_flow,
        (
            "get_payment",
            "mark_refunding",
            "refund_star_payment",
            "mark_refunded",
            "sync",
            "first_charge_of",
            "cancel_star_subscription",
            "send_text",
        ),
    ),
    "on_refunded": (
        refunded_flow,
        ("get_payment", "mark_refunded", "sync", "record_event", "send_text"),
    ),
    "on_subscription": (subscription_flow, ("record_event",)),
}
CASES = [(name, step) for name, (_run, steps) in FLOWS.items() for step in steps]


async def run_with(flow: str, step: str, error: BaseException) -> object:
    """Прогнать сценарий с отказом на шаге и убедиться, что до шага он ДОШЁЛ.

    Без этой проверки матрица зеленеет на пустоте: сценарий, не дошедший до шага (повтор
    апдейта, ранний выход), «переживает» любое исключение, которого не видел.
    """
    payments, ledger, api, slots = desk_with_slots()
    await paid_subscription(payments)
    if step == "sync":
        slots.failure = error
    else:
        (ledger.failures if step in LEDGER_STEPS else api.failures)[step] = error
    ledger.order.clear()
    try:
        return await FLOWS[flow][0](payments)
    finally:
        reached = any(f"{kind}:{step}" in ledger.order for kind in ("ledger", "api", "slots"))
        assert reached, f"сценарий {flow} не дошёл до шага {step}: тест проверял пустоту"


def test_every_port_call_of_the_desk_is_in_the_matrix() -> None:
    """Список шагов связан с кодом механически, а не памятью автора теста.

    Добавили вызов порта в сценарий — без строки в матрице красный этот тест, а не молчание.
    """
    called = set(re.findall(r"self\._(?:ledger|api|slots)\.(\w+)", inspect.getsource(PaymentDesk)))
    in_matrix = {step for _name, step in CASES}

    assert called == in_matrix, (called - in_matrix, in_matrix - called)


@pytest.mark.parametrize(("flow", "step"), CASES)
async def test_a_failure_nobody_has_seen_at_any_step_is_an_outcome_not_a_traceback(
    flow: str, step: str
) -> None:
    """Чужой тип, о котором код не знает: сценарий доходит до конца и отвечает, а не падает."""
    await run_with(flow, step, Failing("чужое"))


@pytest.mark.parametrize("make", INTERRUPTS)
@pytest.mark.parametrize(("flow", "step"), CASES)
async def test_an_interrupt_at_any_step_is_finished_and_then_raised(
    flow: str, step: str, make: Callable[[], BaseException]
) -> None:
    """Прерывание не глотается и не обрывает сценарий на полпути: доделал, потом уступил."""
    with pytest.raises((KeyboardInterrupt, asyncio.CancelledError, BaseExceptionGroup)):
        await run_with(flow, step, make())


@pytest.mark.parametrize(("flow", "step"), CASES)
async def test_a_mixed_group_at_any_step_is_a_failure_not_an_interrupt(
    flow: str, step: str
) -> None:
    mixed = BaseExceptionGroup("g", [asyncio.CancelledError(), Failing("настоящий")])

    await run_with(flow, step, mixed)


@pytest.mark.parametrize("error", [SystemExit(3), GeneratorExit()])
@pytest.mark.parametrize(("flow", "step"), CASES)
async def test_a_request_to_exit_passes_through_every_step(
    flow: str, step: str, error: BaseException
) -> None:
    with pytest.raises(type(error)):
        await run_with(flow, step, error)


async def test_a_failure_in_the_ledger_step_has_its_documented_answer() -> None:
    """Не «что-то пошло не так», а конкретный ответ из таблицы текстов."""
    payments, ledger, _api = desk()
    ledger.failures["record_payment"] = Failing("база")
    assert await reject_flow(payments) == words.payment_unrecorded(48)

    payments, _ledger, api = desk()
    api.failures["refund_star_payment"] = Failing("сеть")
    assert await reject_flow(payments) == words.payment_refund_failed(48)
