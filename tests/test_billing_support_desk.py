"""`/paysupport` и `/support`: вопрос уходит владельцу вместе с платежами клиента.

Обязательное условие Telegram к платным ботам (ToS 6.2.1). Порты — подделки: по ним видно,
что ушло владельцу и что записано в журнал событий.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Callable

import pytest

from sniffer.bot import billing_support
from sniffer.bot import billing_wording as words
from sniffer.bot.billing_support import PER_WINDOW, TEXT_LIMIT, SupportDesk
from sniffer.domain.billing import EventKind, PaymentKind, PaymentRecord
from tests.billing_support import CLIENT, OWNER, Failing, FakeLedger, RecordingApi


def make(*, owner: int = OWNER) -> tuple[SupportDesk, FakeLedger, RecordingApi]:
    order: list[str] = []
    ledger, api = FakeLedger(order), RecordingApi(order)
    return SupportDesk(ledger=ledger, api=api, owner_id=owner, reply_hours=48), ledger, api


async def ask(
    desk: SupportDesk, text: str = "оплатил, а подписка не появилась", command: str = "paysupport"
) -> str:
    return await desk.handle(command=command, tg_user_id=CLIENT, username="dima", text=text)


# ── что отвечаем ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
async def test_a_question_without_text_is_told_how_to_ask(blank: str) -> None:
    desk, ledger, api = make()

    pay = await ask(desk, blank, "paysupport")
    plain = await ask(desk, blank, "support")

    assert pay == words.paysupport_prompt(48) and "/paysupport" in pay
    assert plain == words.support_prompt(48) and "/support" in plain
    assert ledger.calls == [] and api.texts == []


async def test_without_an_owner_there_is_nobody_to_pass_it_to() -> None:
    desk, _ledger, api = make(owner=0)

    assert await ask(desk) == words.SUPPORT_UNAVAILABLE
    assert api.texts == []


async def test_the_question_reaches_the_owner_with_the_clients_payments() -> None:
    desk, ledger, api = make()
    ledger.payments["charge-1"] = _payment()

    reply = await ask(desk)

    assert reply == words.support_sent(48)
    (chat, text) = api.texts[0]
    assert chat == OWNER
    assert str(CLIENT) in text and "@dima" in text and "подписка не появилась" in text
    assert "charge-1" in text, "по нему владелец сразу находит платёж и решает про возврат"
    assert [event.kind for event in ledger.events] == [EventKind.SUPPORT]
    assert ledger.events[0].payload["command"] == "paysupport"


def _payment() -> PaymentRecord:
    return PaymentRecord(
        charge_id="charge-1",
        tg_user_id=CLIENT,
        amount=10,
        currency="XTR",
        kind=PaymentKind.FIRST,
        invoice_payload="v2:s:42:2026-10-03:0123456789ab",
        is_recurring=True,
        is_first_recurring=True,
        period_end=None,
        raw={},
    )


async def test_the_clients_markup_cannot_reach_the_owner_as_markup() -> None:
    desk, _ledger, api = make()

    await desk.handle(
        command="support", tg_user_id=CLIENT, username="<b>x</b>", text="<script>&</script>"
    )

    text = api.texts[0][1]
    assert "<script>" not in text and "<b>x</b>" not in text and "&lt;script&gt;" in text


async def test_a_long_question_is_cut_not_refused() -> None:
    desk, _ledger, api = make()

    await ask(desk, "я" * (TEXT_LIMIT * 3))

    assert "я" * TEXT_LIMIT in api.texts[0][1] and "я" * (TEXT_LIMIT + 1) not in api.texts[0][1]


# ── не завалить владельца ───────────────────────────────────────────────────


async def test_the_same_client_cannot_flood_the_owner() -> None:
    """Публичный бот иначе — способ завалить чат владельца и упереться в лимиты отправки."""
    desk, _ledger, api = make()

    replies = [await ask(desk, f"вопрос {number}") for number in range(PER_WINDOW + 2)]

    assert replies[:PER_WINDOW] == [words.support_sent(48)] * PER_WINDOW
    assert replies[PER_WINDOW:] == [words.SUPPORT_THROTTLED] * 2
    assert len(api.texts) == PER_WINDOW


async def test_a_question_that_did_not_reach_the_owner_does_not_count_against_the_limit() -> None:
    desk, ledger, api = make()
    api.failures["send_text"] = Failing("сеть")

    reply = await ask(desk)

    assert reply == words.SUPPORT_UNAVAILABLE and ledger.events == []


async def test_support_still_works_when_the_ledger_is_down() -> None:
    """Для обращения важнее дойти до владельца, чем быть записанным и посчитанным."""
    desk, ledger, api = make()
    ledger.failures["events_within"] = Failing("база")
    ledger.failures["recent_payments"] = Failing("база")
    ledger.failures["record_event"] = Failing("база")

    reply = await ask(desk)

    assert reply == words.support_sent(48) and len(api.texts) == 1
    assert "платежей нет" in api.texts[0][1]


# ── полнота охраны: чужой тип и прерывание на каждом шаге ────────────────────

LEDGER_STEPS = {"events_within", "recent_payments", "record_event"}
STEPS = ("events_within", "recent_payments", "send_text", "record_event")
INTERRUPTS = [
    pytest.param(KeyboardInterrupt, id="KeyboardInterrupt"),
    pytest.param(asyncio.CancelledError, id="CancelledError"),
    pytest.param(lambda: BaseExceptionGroup("g", [KeyboardInterrupt()]), id="group"),
]


async def run_with(step: str, error: BaseException) -> str:
    """Прогнать обращение с отказом на шаге и убедиться, что до шага оно ДОШЛО."""
    desk, ledger, api = make()
    (ledger.failures if step in LEDGER_STEPS else api.failures)[step] = error
    try:
        return await ask(desk)
    finally:
        reached = f"ledger:{step}" in ledger.order or f"api:{step}" in ledger.order
        assert reached, f"обращение не дошло до шага {step}: тест проверял пустоту"


def test_every_port_call_of_the_desk_is_in_the_matrix() -> None:
    called = set(re.findall(r"self\._(?:ledger|api)\.(\w+)", inspect.getsource(SupportDesk)))

    assert called == set(STEPS), (called, STEPS)


@pytest.mark.parametrize("step", STEPS)
async def test_a_failure_nobody_has_seen_at_any_step_is_an_outcome(step: str) -> None:
    reply = await run_with(step, Failing("чужое"))

    assert reply in {words.support_sent(48), words.SUPPORT_UNAVAILABLE}


@pytest.mark.parametrize("make_error", INTERRUPTS)
@pytest.mark.parametrize("step", STEPS)
async def test_an_interrupt_is_raised_after_the_desk_is_done(
    step: str, make_error: Callable[[], BaseException]
) -> None:
    with pytest.raises((KeyboardInterrupt, asyncio.CancelledError, BaseExceptionGroup)):
        await run_with(step, make_error())


@pytest.mark.parametrize("error", [SystemExit(3), GeneratorExit()])
@pytest.mark.parametrize("step", STEPS)
async def test_a_request_to_exit_passes_through(step: str, error: BaseException) -> None:
    with pytest.raises(type(error)):
        await run_with(step, error)


def test_the_limits_are_the_documented_ones() -> None:
    assert (billing_support.PER_WINDOW, billing_support.TEXT_LIMIT) == (3, 1000)
    assert billing_support.WINDOW.total_seconds() == 3600
