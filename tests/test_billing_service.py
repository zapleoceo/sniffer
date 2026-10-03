"""Покупка подписки и проверка перед списанием: сервис с поддельными портами.

Ни базы, ни Telegram: порты — `FakeLedger` и `RecordingApi` (`tests/billing_support.py`).
Всё, что здесь решается про деньги, видно по вызовам портов, а не по словам.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Callable
from typing import Any

import pytest

from sniffer.bot import billing_service as service_module
from sniffer.bot import billing_wording as words
from sniffer.bot.billing import CheckoutFacts, InvoicePayload
from sniffer.bot.billing_service import BillingService, Confirmation, Verdict, guarded_verdict
from sniffer.domain import plans
from sniffer.domain.billing import Reason
from tests.billing_support import CLIENT, NONCE, OWNER, Failing, FakeLedger, RecordingApi

PAYLOAD = InvoicePayload(CLIENT, words.TERMS_VERSION, NONCE).encode()


def make(*, owner: int = OWNER, live: int = 0) -> tuple[BillingService, FakeLedger, RecordingApi]:
    order: list[str] = []
    ledger, api = FakeLedger(order), RecordingApi(order)
    ledger.live = live
    return BillingService(ledger=ledger, api=api, owner_id=owner, nonce=lambda: NONCE), ledger, api


# ── экран «Подписка» ────────────────────────────────────────────────────────


async def test_the_screen_names_the_numbers_before_any_link_exists() -> None:
    sales, _ledger, api = make(live=0)

    screen = await sales.confirmation(CLIENT)

    assert screen.offer
    assert screen.text == words.confirmation(0)
    assert api.links == [], "ссылки ещё нет: сначала цифры и согласие"


async def test_the_screen_says_how_many_subscriptions_the_client_already_has() -> None:
    """Вторую подписку случайно не покупают: человек видит, сколько у него уже есть."""
    sales, _ledger, _api = make(live=2)

    screen = await sales.confirmation(CLIENT)

    assert "2 подписки" in screen.text and "не изменится" in screen.text


async def test_without_an_owner_nothing_is_sold() -> None:
    """Некому вернуть звёзды и ответить на `/paysupport` — а это условие Telegram."""
    sales, ledger, api = make(owner=0)

    screen = await sales.confirmation(CLIENT)
    link = await sales.issue_link(CLIENT)

    assert (screen.text, screen.offer, link) == (words.BILLING_OFF, False, None)
    assert ledger.calls == [] and api.order == []


async def test_a_broken_ledger_is_an_honest_unavailable_not_a_wrong_number() -> None:
    sales, ledger, _api = make()
    ledger.failures["live_subscriptions"] = Failing("база")

    screen = await sales.confirmation(CLIENT)

    assert (screen.text, screen.offer) == (words.UNAVAILABLE, False)


# ── ссылка на счёт ──────────────────────────────────────────────────────────


async def test_the_link_is_created_for_the_tariff_with_the_period_telegram_accepts() -> None:
    sales, _ledger, api = make(live=2)

    link = await sales.issue_link(CLIENT)

    assert link is not None and link.url == "https://t.me/$test-invoice-link"
    (call,) = api.links
    assert call["title"] == "Слежение №3", "номер подписки после покупки"
    assert (call["amount"], call["period_s"]) == (plans.SUBSCRIPTION_STARS, 2_592_000)
    assert call["payload"] == PAYLOAD
    assert len(call["description"]) <= 255 and len(call["title"]) <= 32


async def test_the_consent_is_recorded_before_the_link_exists() -> None:
    """Ссылка без записанного согласия — покупка, в которой не доказать, что условия прочитаны."""
    sales, ledger, api = make()

    await sales.issue_link(CLIENT)

    assert ledger.order.index("ledger:record_consent") < api.order.index("api:create_invoice_link")
    assert ledger.consents == {(CLIENT, words.TERMS_DOC, words.TERMS_VERSION)}


async def test_no_consent_means_no_link() -> None:
    sales, ledger, api = make()
    ledger.failures["record_consent"] = Failing("база")

    link = await sales.issue_link(CLIENT)

    assert link is None and api.links == []


async def test_a_telegram_refusal_means_no_link_but_the_consent_stays() -> None:
    sales, ledger, api = make()
    api.failures["create_invoice_link"] = Failing("Bad Request")

    link = await sales.issue_link(CLIENT)

    assert link is None and ledger.consents


async def test_every_link_is_a_different_payload() -> None:
    """Продление и события подписки находят её по нагрузке: две подписки не должны её делить."""
    nonces = iter(["aaaaaaaaaaaa", "bbbbbbbbbbbb"])
    ledger, api = FakeLedger(), RecordingApi()
    sales = BillingService(ledger=ledger, api=api, owner_id=OWNER, nonce=lambda: next(nonces))

    await sales.issue_link(CLIENT)
    await sales.issue_link(CLIENT)

    assert len({call["payload"] for call in api.links}) == 2


# ── до списания ─────────────────────────────────────────────────────────────


def checkout(payload: str = PAYLOAD, **overrides: object) -> CheckoutFacts:
    fields: dict[str, object] = {
        "buyer_id": CLIENT,
        "currency": "XTR",
        "total_amount": 10,
        "payload": payload,
    }
    fields.update(overrides)
    return CheckoutFacts(**fields)  # type: ignore[arg-type]


async def agreed() -> BillingService:
    sales, ledger, _api = make()
    ledger.consents.add((CLIENT, words.TERMS_DOC, words.TERMS_VERSION))
    return sales


async def test_a_good_invoice_passes() -> None:
    sales = await agreed()

    assert await sales.pre_checkout(checkout()) == Verdict(True)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"total_amount": 1}, Reason.WRONG_AMOUNT),
        ({"currency": "USD"}, Reason.WRONG_CURRENCY),
        ({"buyer_id": 777}, Reason.FOREIGN_BUYER),
        ({"payload": "sub:42"}, Reason.LEGACY_INVOICE),
        ({"payload": "чужое"}, Reason.BAD_PAYLOAD),
    ],
)
async def test_a_bad_invoice_is_refused_with_a_reason(
    overrides: dict[str, Any], reason: Reason
) -> None:
    sales = await agreed()

    verdict = await sales.pre_checkout(checkout(**overrides))

    assert verdict == Verdict(False, reason)
    assert verdict.message == words.refusal(reason)


async def test_an_invoice_without_the_clients_consent_is_refused() -> None:
    sales, _ledger, _api = make()

    assert await sales.pre_checkout(checkout()) == Verdict(False, Reason.NO_CONSENT)


async def test_consent_is_checked_for_the_version_in_the_invoice_not_the_current_one() -> None:
    """Если Telegram присылает `pre_checkout` и на автопродление, правка условий не срывает его.

    Согласие с версией из счёта записано, пока человек покупал; текущая версия могла уйти вперёд.
    """
    sales, ledger, _api = make()
    old = InvoicePayload(CLIENT, "2025-01-01", NONCE).encode()
    ledger.consents.add((CLIENT, words.TERMS_DOC, "2025-01-01"))

    assert await sales.pre_checkout(checkout(old)) == Verdict(True)
    assert await sales.pre_checkout(checkout()) == Verdict(False, Reason.NO_CONSENT)


async def test_a_refusal_never_reads_the_database_when_the_invoice_is_already_wrong() -> None:
    """Дешёвое решение — раньше дорогого: на чужой счёт база не нужна."""
    sales, ledger, _api = make()

    await sales.pre_checkout(checkout(total_amount=1))
    await sales.pre_checkout(checkout("чужое"))

    assert ledger.calls == []


# ── окно в десять секунд ────────────────────────────────────────────────────


def test_the_budget_leaves_room_inside_the_ten_seconds_telegram_gives() -> None:
    assert service_module.PRE_CHECKOUT_BUDGET_S == 8 < 10


async def test_the_verdict_is_always_there_for_a_failure_it_has_never_seen() -> None:
    async def boom() -> Verdict:
        raise Failing("чужое исключение")

    verdict = await guarded_verdict(boom)

    assert verdict == Verdict(False, Reason.UNAVAILABLE)


async def test_a_hanging_database_is_a_refusal_within_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Зависание базы дольше бюджета — отказ «повторите», а не неотвеченный запрос."""
    monkeypatch.setattr(service_module, "PRE_CHECKOUT_BUDGET_S", 0.05)

    async def hang() -> Verdict:
        await asyncio.sleep(3600)
        return Verdict(True)

    verdict = await asyncio.wait_for(guarded_verdict(hang), timeout=5)

    assert verdict == Verdict(False, Reason.TIMEOUT)


@pytest.mark.parametrize(
    "make",
    [
        KeyboardInterrupt,
        asyncio.CancelledError,
        lambda: BaseExceptionGroup("g", [KeyboardInterrupt()]),
    ],
    ids=["KeyboardInterrupt", "CancelledError", "group"],
)
async def test_an_interrupt_is_refused_and_carried_up_not_swallowed(
    make: Callable[[], BaseException],
) -> None:
    error = make()

    async def interrupted() -> Verdict:
        raise error

    verdict = await guarded_verdict(interrupted)

    assert verdict.ok is False and verdict.reason is Reason.INTERRUPTED
    assert verdict.interruption is error


async def test_a_mixed_group_is_a_failure_not_an_interrupt() -> None:
    mixed = BaseExceptionGroup("g", [asyncio.CancelledError(), Failing("настоящий")])

    async def work() -> Verdict:
        raise mixed

    verdict = await guarded_verdict(work)

    assert verdict == Verdict(False, Reason.UNAVAILABLE)


@pytest.mark.parametrize("error", [SystemExit(2), GeneratorExit()])
async def test_a_request_to_exit_is_not_turned_into_a_refusal(error: BaseException) -> None:
    async def work() -> Verdict:
        raise error

    with pytest.raises(type(error)):
        await guarded_verdict(work)


def test_a_refusal_without_a_reason_cannot_exist() -> None:
    """Клиенту в окне оплаты нечего было бы сказать."""
    with pytest.raises(ValueError, match="без причины"):
        Verdict(False)


# ── полнота охраны покупки: чужой тип и прерывание на каждом шаге ────────────

LEDGER_STEPS = {"live_subscriptions", "record_consent"}
PURCHASE_STEPS = ("live_subscriptions", "record_consent", "create_invoice_link")
INTERRUPTS = [
    pytest.param(KeyboardInterrupt, id="KeyboardInterrupt"),
    pytest.param(asyncio.CancelledError, id="CancelledError"),
    pytest.param(lambda: BaseExceptionGroup("g", [KeyboardInterrupt()]), id="group"),
]


async def buy_with(flow: str, step: str, error: BaseException) -> object:
    """Покупка с отказом на шаге; до шага она обязана ДОЙТИ, иначе тест проверял пустоту."""
    sales, ledger, api = make()
    (ledger.failures if step in LEDGER_STEPS else api.failures)[step] = error
    try:
        if flow == "confirmation":
            return await sales.confirmation(CLIENT)
        return await sales.issue_link(CLIENT)
    finally:
        reached = f"ledger:{step}" in ledger.order or f"api:{step}" in ledger.order
        assert reached, f"{flow} не дошёл до шага {step}"


PURCHASE_CASES = [("confirmation", "live_subscriptions")] + [
    ("issue_link", step) for step in PURCHASE_STEPS
]


def test_every_port_call_of_the_purchase_is_in_the_matrix() -> None:
    called = set(re.findall(r"self\._(?:ledger|api)\.(\w+)", inspect.getsource(BillingService)))

    assert called == set(PURCHASE_STEPS), (called, PURCHASE_STEPS)


@pytest.mark.parametrize(("flow", "step"), PURCHASE_CASES)
async def test_an_unseen_failure_at_any_purchase_step_is_a_documented_answer(
    flow: str, step: str
) -> None:
    answer = await buy_with(flow, step, Failing("чужое"))

    assert answer is None or answer == Confirmation(words.UNAVAILABLE, offer=False)


@pytest.mark.parametrize("make_error", INTERRUPTS)
@pytest.mark.parametrize(("flow", "step"), PURCHASE_CASES)
async def test_an_interrupt_at_any_purchase_step_is_raised_not_swallowed(
    flow: str, step: str, make_error: Callable[[], BaseException]
) -> None:
    with pytest.raises((KeyboardInterrupt, asyncio.CancelledError, BaseExceptionGroup)):
        await buy_with(flow, step, make_error())


@pytest.mark.parametrize("error", [SystemExit(3), GeneratorExit()])
@pytest.mark.parametrize(("flow", "step"), PURCHASE_CASES)
async def test_a_request_to_exit_passes_through_the_purchase(
    flow: str, step: str, error: BaseException
) -> None:
    with pytest.raises(type(error)):
        await buy_with(flow, step, error)
