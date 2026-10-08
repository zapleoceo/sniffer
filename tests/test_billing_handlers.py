"""Оплата на настоящих апдейтах: `Dispatcher.feed_update(bot, Update.model_validate(json))`.

Ни одного фейка с `**kwargs`: апдейты — настоящие `Update`, бот — настоящий `Bot` с сессией,
которая записывает каждый вызов как `TelegramMethod`, ответы идут через настоящие типы aiogram.
Журнал — подделка (`FakeLedger`), потому что база — отдельная забота (`test_billing_repository`).
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    AnswerPreCheckoutQuery,
    CreateInvoiceLink,
    EditMessageReplyMarkup,
    EditMessageText,
    EditUserStarSubscription,
    RefundStarPayment,
    SendInvoice,
    SendMessage,
)
from aiogram.types import InlineKeyboardMarkup

from sniffer.bot import app as bot_app
from sniffer.bot import billing_service
from sniffer.bot import billing_wording as words
from sniffer.bot.billing import InvoicePayload
from sniffer.bot.billing_payments import PaymentDesk
from sniffer.bot.billing_service import BillingService
from sniffer.bot.billing_support import SupportDesk
from sniffer.bot.billing_telegram import AiogramBotApi
from sniffer.bot.billing_ui import BillingCallback
from sniffer.bot.handlers import billing as billing_handlers
from sniffer.bot.handlers import search
from sniffer.domain.billing import Reason
from tests import billing_support as fx
from tests.billing_support import CLIENT, NONCE, OWNER, FakeLedger, FakeSlots, RecordingSession

PAYLOAD = InvoicePayload(CLIENT, words.TERMS_VERSION, NONCE).encode()
ACCEPT = BillingCallback(action="accept", version=words.TERMS_VERSION).pack()


class SearchSpy:
    """Вместо диалога: считает, сколько раз текст дошёл до поиска."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def on_text(self, _client: object, text: str, _send: object) -> None:
        self.texts.append(text)


@dataclass
class Wired:
    bot: Bot
    session: RecordingSession
    ledger: FakeLedger
    search: SearchSpy
    dispatcher: Dispatcher

    async def feed(self, update: Any) -> None:
        await self.dispatcher.feed_update(self.bot, update)

    def answers(self) -> list[AnswerPreCheckoutQuery]:
        return self.session.sent(AnswerPreCheckoutQuery)

    def messages(self) -> list[SendMessage]:
        return self.session.sent(SendMessage)


def wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[Wired]:
    """Собрать обвязку. Отдельной функцией: фикстуру можно объявить и в соседнем файле."""
    ledger, session = FakeLedger(), RecordingSession()
    bot, spy = fx.make_bot(session), SearchSpy()

    def desks(for_bot: Bot) -> billing_handlers.Desks:
        api = AiogramBotApi(for_bot)
        return billing_handlers.Desks(
            sales=BillingService(
                ledger=ledger, api=api, owner_id=OWNER, sales_enabled=True, nonce=lambda: NONCE
            ),
            payments=PaymentDesk(
                ledger=ledger, api=api, slots=FakeSlots(), owner_id=OWNER, reply_hours=48
            ),
            support=SupportDesk(ledger=ledger, api=api, owner_id=OWNER, reply_hours=48),
        )

    monkeypatch.setattr(billing_handlers, "desks", desks)
    monkeypatch.setattr(search, "conversation", lambda: spy)
    billing_handlers._issued.clear()
    yield Wired(bot, session, ledger, spy, bot_app.build_dispatcher())
    billing_handlers._issued.clear()


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Iterator[Wired]:
    yield from wire(monkeypatch)


def keyboard_of(call: SendMessage) -> InlineKeyboardMarkup:
    assert isinstance(call.reply_markup, InlineKeyboardMarkup)
    return call.reply_markup


# ── команды: до диалога, а не вместо поиска ─────────────────────────────────

COMMANDS = ["/subscription", "/terms", "/support", "/paysupport", "/refund x"]


@pytest.mark.parametrize("text", COMMANDS)
async def test_a_billing_command_is_answered_and_never_becomes_a_search(
    wired: Wired, text: str
) -> None:
    """Любая неизвестная команда уходила в поиск как поисковая фраза, а `/paysupport` обязателен."""
    await wired.feed(fx.command(text, from_id=OWNER if text.startswith("/refund") else CLIENT))

    assert wired.search.texts == [], "команда дошла до диалога"
    assert wired.messages(), f"{text} осталась без ответа"


async def test_an_ordinary_message_still_reaches_the_search(wired: Wired) -> None:
    """Контроль для теста выше: заглушка поиска живая, а порядок роутеров его не отрезал."""
    update = fx.update(1, message=fx.message(text="ищу скутер в Нячанге"))

    await wired.feed(update)

    assert wired.search.texts == ["ищу скутер в Нячанге"]


async def test_the_subscription_screen_has_the_numbers_and_three_buttons(wired: Wired) -> None:
    await wired.feed(fx.command("/subscription"))

    (sent,) = wired.messages()
    assert sent.text == words.confirmation(0)
    labels = [button.text for row in keyboard_of(sent).inline_keyboard for button in row]
    assert labels == [words.ACCEPT_LABEL, words.TERMS_LABEL, words.CANCEL_LABEL]
    accept = BillingCallback.unpack(str(keyboard_of(sent).inline_keyboard[0][0].callback_data))
    assert (accept.action, accept.version) == ("accept", words.TERMS_VERSION), (
        "согласие записывается ровно с тем текстом, который человек видел на экране"
    )
    assert wired.session.sent(CreateInvoiceLink) == [], "ссылки ещё нет: сначала цифры и согласие"


async def test_the_terms_are_one_command_away(wired: Wired) -> None:
    await wired.feed(fx.command("/terms"))

    assert wired.messages()[0].text == words.terms(48)


async def test_a_question_to_support_reaches_the_owner_and_the_client_is_answered(
    wired: Wired,
) -> None:
    await wired.feed(fx.command("/paysupport оплатил, а подписка не появилась"))

    chats = [message.chat_id for message in wired.messages()]
    assert chats == [OWNER, CLIENT]
    assert "подписка не появилась" in str(wired.messages()[0].text)
    assert wired.messages()[1].text == words.support_sent(48)


async def test_a_bare_support_command_explains_how_to_ask(wired: Wired) -> None:
    await wired.feed(fx.command("/paysupport"))

    assert [m.text for m in wired.messages()] == [words.paysupport_prompt(48)]


async def test_refund_is_for_the_owner_only_and_a_stranger_gets_silence(wired: Wired) -> None:
    await wired.feed(fx.command("/refund charge-1 42", from_id=CLIENT))

    assert wired.messages() == [] and wired.search.texts == []
    assert wired.session.calls == [], "чужой даже не узнаёт, что команда есть"


# ── кнопки: согласие, ссылка, устаревшее ────────────────────────────────────


async def test_accepting_the_terms_records_consent_and_hands_out_a_subscription_link(
    wired: Wired,
) -> None:
    await wired.feed(fx.callback(ACCEPT))

    (invoice,) = wired.session.sent(CreateInvoiceLink)
    assert invoice.subscription_period == 2_592_000 and invoice.currency == "XTR"
    assert invoice.payload == PAYLOAD and invoice.provider_token is None
    assert [(p.label, p.amount) for p in invoice.prices] == [(words.INVOICE_LABEL, 10)]
    assert wired.ledger.consents == {(CLIENT, words.TERMS_DOC, words.TERMS_VERSION)}
    (sent,) = wired.messages()
    pay = keyboard_of(sent).inline_keyboard[0][0]
    assert (pay.text, pay.url) == (words.PAY_LABEL, fx.LINK), "ссылка — кнопкой-адресом, не Pay"
    assert wired.session.sent(SendInvoice) == [], "счёт-сообщением подписку не продать"
    assert len(wired.session.sent(EditMessageReplyMarkup)) == 1, "кнопка «принимаю» снята"


async def test_a_double_tap_does_not_make_a_second_link(wired: Wired) -> None:
    """Две ссылки — две подписки, оплаченные по ошибке: Telegram параллельные разрешает."""
    await wired.feed(fx.callback(ACCEPT, update_id=1))
    await wired.feed(fx.callback(ACCEPT, update_id=2))

    assert len(wired.session.sent(CreateInvoiceLink)) == 1
    answers = wired.session.sent(AnswerCallbackQuery)
    assert answers[-1].text == words.ALREADY_ISSUED


async def test_a_failed_link_releases_the_button_so_the_client_can_try_again(
    wired: Wired,
) -> None:
    wired.session.failures[CreateInvoiceLink] = TelegramBadRequest(
        method=CreateInvoiceLink(
            title="t", description="d", payload="p", currency="XTR", prices=[]
        ),
        message="Bad Request: boom",
    )
    await wired.feed(fx.callback(ACCEPT, update_id=1))
    assert [m.text for m in wired.messages()] == [words.UNAVAILABLE]

    del wired.session.failures[CreateInvoiceLink]
    await wired.feed(fx.callback(ACCEPT, update_id=2))

    assert wired.messages()[-1].text == words.link_ready(1)


async def test_a_link_that_could_not_be_sent_releases_the_button(wired: Wired) -> None:
    wired.session.failures[SendMessage] = TelegramBadRequest(
        method=SendMessage(chat_id=1, text="x"), message="Bad Request: boom"
    )
    with pytest.raises(TelegramBadRequest):
        await wired.feed(fx.callback(ACCEPT, update_id=1))

    del wired.session.failures[SendMessage]
    await wired.feed(fx.callback(ACCEPT, update_id=2))

    assert wired.messages()[-1].text == words.link_ready(1)


async def test_accepting_old_terms_shows_the_new_ones_instead_of_a_link(wired: Wired) -> None:
    old = BillingCallback(action="accept", version="2020-01-01").pack()

    await wired.feed(fx.callback(old))

    assert wired.session.sent(CreateInvoiceLink) == [] and wired.ledger.consents == set()
    alert = wired.session.sent(AnswerCallbackQuery)[0]
    assert alert.text == words.TERMS_CHANGED and alert.show_alert is True
    assert wired.messages()[0].text == words.confirmation(0), "и тут же новый экран"


@pytest.mark.parametrize(
    "data",
    [ACCEPT, BillingCallback(action="terms").pack()],
    ids=["accept", "terms"],
)
async def test_a_stale_button_says_so_instead_of_silence(wired: Wired, data: str) -> None:
    """Сообщение старше 48 часов Telegram отдаёт недоступным; на деньгах молчать нельзя."""
    await wired.feed(fx.stale_callback(data))

    (answer,) = wired.session.sent(AnswerCallbackQuery)
    assert answer.text == words.STALE_BUTTON and answer.show_alert is True
    assert wired.messages() == [] and wired.session.sent(CreateInvoiceLink) == []


async def test_cancel_and_terms_buttons(wired: Wired) -> None:
    await wired.feed(fx.callback(BillingCallback(action="terms").pack(), update_id=1))
    await wired.feed(fx.callback(BillingCallback(action="cancel").pack(), update_id=2))

    assert wired.messages()[0].text == words.terms(48)
    assert wired.session.sent(CreateInvoiceLink) == []
    edited = wired.session.sent(EditMessageText)
    assert [call.text for call in edited] == [words.CANCELLED]


async def test_an_unknown_button_action_is_a_stale_button_not_a_crash(wired: Wired) -> None:
    await wired.feed(fx.callback(BillingCallback(action="что-то").pack()))

    assert wired.session.sent(AnswerCallbackQuery)[0].text == words.STALE_BUTTON


# ── платёжные апдейты ───────────────────────────────────────────────────────


async def test_a_paid_subscription_is_recorded_and_thanked(wired: Wired) -> None:
    await wired.feed(fx.successful_payment(PAYLOAD))

    assert wired.ledger.payments["charge-1"].kind.value == "first"
    (sent,) = wired.messages()
    assert sent.chat_id == CLIENT and str(sent.text).startswith("Оплата получена")


async def test_a_payment_that_is_not_ours_is_refunded_and_everyone_is_told(wired: Wired) -> None:
    await wired.feed(fx.successful_payment(PAYLOAD, amount=1))

    (refund,) = wired.session.sent(RefundStarPayment)
    assert (refund.user_id, refund.telegram_payment_charge_id) == (CLIENT, "charge-1")
    (cancel,) = wired.session.sent(EditUserStarSubscription)
    assert cancel.is_canceled is True and cancel.telegram_payment_charge_id == "charge-1"
    assert [m.chat_id for m in wired.messages()] == [OWNER, CLIENT]


async def test_a_refund_message_and_a_subscription_update_are_journaled(wired: Wired) -> None:
    await wired.feed(fx.successful_payment(PAYLOAD))
    wired.session.calls.clear()

    await wired.feed(fx.refunded_payment(PAYLOAD))
    await wired.feed(fx.subscription_changed(PAYLOAD, "canceled"))

    kinds = [event.kind.value for event in wired.ledger.events]
    assert kinds == ["refunded", "refund_synced", "renewal_canceled", "sub_canceled"]
    assert "charge-1" in wired.ledger.refunded


# ── pre_checkout: ровно один ответ, всегда ──────────────────────────────────


def agree(wired: Wired) -> None:
    wired.ledger.consents.add((CLIENT, words.TERMS_DOC, words.TERMS_VERSION))


async def one_answer(wired: Wired) -> AnswerPreCheckoutQuery:
    assert len(wired.answers()) == 1, f"ответов на pre_checkout: {len(wired.answers())}"
    return wired.answers()[0]


async def test_a_good_checkout_is_approved_exactly_once(wired: Wired) -> None:
    agree(wired)

    await wired.feed(fx.pre_checkout(PAYLOAD))

    answer = await one_answer(wired)
    assert answer.ok is True and answer.pre_checkout_query_id == "pcq-1"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"from_id": 777}, Reason.FOREIGN_BUYER),
        ({"amount": 1}, Reason.WRONG_AMOUNT),
        ({"amount": 11}, Reason.WRONG_AMOUNT),
        ({"currency": "USD"}, Reason.WRONG_CURRENCY),
    ],
    ids=["foreign-buyer", "cheaper", "dearer", "other-currency"],
)
async def test_a_checkout_that_does_not_fit_is_refused_with_words(
    wired: Wired, kwargs: dict[str, Any], reason: Reason
) -> None:
    """Деньги ещё не сняты: отказ ничего не стоит клиенту, а Telegram покажет ему эти слова."""
    agree(wired)

    await wired.feed(fx.pre_checkout(PAYLOAD, **kwargs))

    answer = await one_answer(wired)
    assert answer.ok is False and answer.error_message == words.refusal(reason)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ("sub:42", Reason.LEGACY_INVOICE),
        ("v9:s:42:2026-10-03:" + NONCE, Reason.BAD_PAYLOAD),
        ("v2:s:²:2026-10-03:" + NONCE, Reason.BAD_PAYLOAD),
        ("", Reason.BAD_PAYLOAD),
    ],
    ids=["old-per-topic-invoice", "unknown-version", "unicode-digit", "empty"],
)
async def test_an_invoice_we_did_not_issue_is_refused(
    wired: Wired, payload: str, reason: Reason
) -> None:
    await wired.feed(fx.pre_checkout(payload))

    answer = await one_answer(wired)
    assert answer.ok is False and answer.error_message == words.refusal(reason)


async def test_an_invoice_without_the_clients_consent_is_refused(wired: Wired) -> None:
    await wired.feed(fx.pre_checkout(PAYLOAD))

    assert (await one_answer(wired)).error_message == words.refusal(Reason.NO_CONSENT)


async def test_a_database_that_raises_is_a_refusal_to_retry_not_silence(wired: Wired) -> None:
    wired.ledger.failures["has_consent"] = ConnectionError("база недоступна")

    await wired.feed(fx.pre_checkout(PAYLOAD))

    answer = await one_answer(wired)
    assert answer.ok is False and answer.error_message == words.refusal(Reason.UNAVAILABLE)


async def test_a_database_that_hangs_is_a_refusal_within_the_budget(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Зависание базы дольше бюджета: ответ уходит, пока у Telegram ещё есть его десять секунд."""
    monkeypatch.setattr(billing_service, "PRE_CHECKOUT_BUDGET_S", 0.05)
    wired.ledger.hangs.add("has_consent")

    await asyncio.wait_for(wired.feed(fx.pre_checkout(PAYLOAD)), timeout=5)

    answer = await one_answer(wired)
    assert answer.ok is False and answer.error_message == words.refusal(Reason.TIMEOUT)


# ── полнота охраны окна: чужой тип и прерывание на КАЖДОМ шаге ────────────────

# Шаги решения `pre_checkout`: четыре проверки сервиса и сборка самого сервиса. Список
# связан с кодом механически: `test_every_step_of_pre_checkout_is_in_the_matrix` сверяет его
# с исходником, чтобы новый шаг без строки здесь краснил тест, а не оставался непроверенным.
SERVICE_STEPS = ("step_payload", "step_buyer", "step_price", "step_consent")
ALL_STEPS = (*SERVICE_STEPS, "desks")
INTERRUPTS = [
    pytest.param(KeyboardInterrupt, id="KeyboardInterrupt"),
    pytest.param(asyncio.CancelledError, id="CancelledError"),
    pytest.param(lambda: BaseExceptionGroup("g", [KeyboardInterrupt()]), id="group"),
]


def fail_at(
    wired: Wired, monkeypatch: pytest.MonkeyPatch, step: str, error: BaseException
) -> list[str]:
    """Подложить исключение в шаг. Возвращает журнал: дошёл ли до шага запрос."""
    reached: list[str] = []

    def boom(*_args: object, **_kwargs: object) -> None:
        reached.append(step)
        raise error

    async def aboom(*_args: object, **_kwargs: object) -> None:
        boom()

    if step == "desks":
        monkeypatch.setattr(billing_handlers, "desks", boom)
    elif step == "step_consent":
        monkeypatch.setattr(billing_service, step, aboom)
    else:
        monkeypatch.setattr(billing_service, step, boom)
    agree(wired)
    return reached


def test_every_step_of_pre_checkout_is_in_the_matrix() -> None:
    """Число шагов в матрице сверяется с исходником функции, а не с памятью автора теста."""
    body = inspect.getsource(BillingService.pre_checkout)
    called = re.findall(r"\bstep_\w+\(", body)

    assert sorted(name.rstrip("(") for name in called) == sorted(SERVICE_STEPS)
    assert inspect.getsource(billing_handlers._decide).count("desks(") == 1
    assert "except Exception" not in body + inspect.getsource(billing_handlers.pre_checkout)


@pytest.mark.parametrize("step", ALL_STEPS)
async def test_a_failure_nobody_has_seen_at_any_step_is_one_refusal_to_retry(
    wired: Wired, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """Чужой тип, о котором код не знает, на любом шаге: один ответ, отказ с просьбой повторить."""
    reached = fail_at(wired, monkeypatch, step, fx.Failing("чужое"))

    await wired.feed(fx.pre_checkout(PAYLOAD))

    assert reached == [step], "запрос не дошёл до шага: тест проверял пустоту"
    answer = await one_answer(wired)
    assert answer.ok is False and answer.error_message == words.refusal(Reason.UNAVAILABLE)


@pytest.mark.parametrize("make", INTERRUPTS)
@pytest.mark.parametrize("step", ALL_STEPS)
async def test_an_interrupt_at_any_step_is_answered_once_and_then_raised(
    wired: Wired, monkeypatch: pytest.MonkeyPatch, step: str, make: Callable[[], BaseException]
) -> None:
    """Прерывание: клиент получает отказ (звёзды не сняты), а остановка процесса не ломается."""
    reached = fail_at(wired, monkeypatch, step, make())

    with pytest.raises((KeyboardInterrupt, asyncio.CancelledError, BaseExceptionGroup)):
        await wired.feed(fx.pre_checkout(PAYLOAD))

    assert reached == [step]
    answer = await one_answer(wired)
    assert answer.ok is False and answer.error_message == words.refusal(Reason.INTERRUPTED)


@pytest.mark.parametrize("step", ALL_STEPS)
async def test_a_mixed_group_at_any_step_is_a_failure_and_is_not_raised(
    wired: Wired, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    mixed = BaseExceptionGroup("g", [asyncio.CancelledError(), fx.Failing("настоящий")])
    fail_at(wired, monkeypatch, step, mixed)

    await wired.feed(fx.pre_checkout(PAYLOAD))

    assert (await one_answer(wired)).error_message == words.refusal(Reason.UNAVAILABLE)


@pytest.mark.parametrize("error", [SystemExit(3), GeneratorExit()])
@pytest.mark.parametrize("step", ALL_STEPS)
async def test_a_request_to_exit_is_not_rewritten_into_an_answer(
    wired: Wired, monkeypatch: pytest.MonkeyPatch, step: str, error: BaseException
) -> None:
    fail_at(wired, monkeypatch, step, error)

    with pytest.raises(type(error)):
        await wired.feed(fx.pre_checkout(PAYLOAD))
