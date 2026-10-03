"""Контракт с Bot API: каждый вызов оплаты — настоящий метод со схемой, без лишних полей.

Прежний счёт уходил `message.answer_invoice(..., subscription_period=2592000)`: у `sendInvoice`
такого параметра нет, aiogram молча пропускал его как «лишнее поле» запроса, а фейк теста,
принимавший `**kwargs`, зеленел на этой ошибке. Здесь заглушка сессии записывает КАЖДЫЙ
`TelegramMethod`, и поле вне схемы метода (`method.model_extra`) роняет тест.

Схема метода — справочник Bot API (читано 03.10.2026): `subscription_period` описан у
`createInvoiceLink` и не описан у `sendInvoice`.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
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
    SetMyCommands,
    TelegramMethod,
)
from aiogram.types import LabeledPrice

from sniffer.bot import app as bot_app
from sniffer.bot import billing_wording as words
from sniffer.bot.billing_ui import BillingCallback
from tests import billing_support as fx
from tests.billing_support import CLIENT, NONCE, OWNER, RecordingSession
from tests.test_billing_handlers import Wired, wire

TESTS = Path(__file__).parent


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Iterator[Wired]:
    yield from wire(monkeypatch)


EXPECTED = {
    CreateInvoiceLink,
    AnswerPreCheckoutQuery,
    RefundStarPayment,
    EditUserStarSubscription,
    SendMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    AnswerCallbackQuery,
    SetMyCommands,
}


def extra_fields(method: TelegramMethod[Any]) -> dict[str, Any]:
    """Поля, которых нет в схеме метода: aiogram не отвергает их, а отправляет."""
    return dict(method.model_extra or {})


@cache
def _session() -> AiohttpSession:
    """Одна на весь файл: загрузка сертификатов у `AiohttpSession` стоит около двух секунд."""
    return AiohttpSession()


def wire_fields(bot: Bot, method: TelegramMethod[Any]) -> dict[str, str]:
    """Что реально уходит на провод: то же, что собирает `AiohttpSession.build_form_data`."""
    form = _session().build_form_data(bot=bot, method=method)
    return {str(options["name"]): str(value) for options, _headers, value in form._fields}


# ── у какого метода какие поля ──────────────────────────────────────────────


def test_the_subscription_period_belongs_to_the_link_method_and_not_to_send_invoice() -> None:
    assert "subscription_period" in CreateInvoiceLink.model_fields
    assert "subscription_period" not in SendInvoice.model_fields


def test_the_old_invoice_message_would_have_been_caught_by_the_rule() -> None:
    """Проверяющий не должен быть всеядным: старая форма запроса обязана его краснить."""
    old = SendInvoice(
        chat_id=CLIENT,
        title="Слежу за новыми",
        description="d",
        payload="sub:7",
        currency="XTR",
        prices=[LabeledPrice(label="месяц слежения", amount=1)],
        subscription_period=2_592_000,
        provider_token="",
    )

    assert extra_fields(old) == {"subscription_period": 2_592_000}
    assert "subscription_period" in wire_fields(fx.make_bot(), old), (
        "и это поле уходило на сервер: он мог проигнорировать его (разовый платёж) или отвергнуть"
    )


# ── весь путь оплаты через настоящие хендлеры ────────────────────────────────


async def drive_everything(wired: Wired) -> None:
    await wired.feed(fx.command("/subscription", update_id=1))
    await wired.feed(fx.callback(BillingCallback(action="terms").pack(), update_id=2))
    await wired.feed(fx.callback(BillingCallback(action="cancel").pack(), update_id=3))
    await wired.feed(
        fx.callback(
            BillingCallback(action="accept", version=words.TERMS_VERSION).pack(), update_id=4
        )
    )
    payload = f"v2:s:{CLIENT}:{words.TERMS_VERSION}:{NONCE}"
    await wired.feed(fx.pre_checkout(payload, update_id=5))
    await wired.feed(fx.successful_payment(payload, amount=1, update_id=6))
    await wired.feed(fx.successful_payment(payload, charge="charge-9", update_id=7))
    await wired.feed(fx.command("/refund charge-9", from_id=OWNER, update_id=8))
    await wired.feed(fx.command("/paysupport не пришла подписка", update_id=9))
    await bot_app.publish_commands(wired.bot)


async def test_every_call_of_the_payment_path_matches_the_schema_of_its_method(
    wired: Wired,
) -> None:
    await drive_everything(wired)

    offenders = {type(call).__name__: extra_fields(call) for call in wired.session.calls}
    assert {name: extra for name, extra in offenders.items() if extra} == {}
    assert {type(call) for call in wired.session.calls} >= EXPECTED, EXPECTED - {
        type(call) for call in wired.session.calls
    }


async def test_the_subscription_link_goes_out_the_way_the_bot_api_describes_it(
    wired: Wired,
) -> None:
    await wired.feed(
        fx.callback(BillingCallback(action="accept", version=words.TERMS_VERSION).pack())
    )

    (call,) = wired.session.sent(CreateInvoiceLink)
    fields = wire_fields(wired.bot, call)
    assert fields["subscription_period"] == "2592000"
    assert fields["currency"] == "XTR"
    assert re.fullmatch(r'\[\{"label": "[^"]+", "amount": 10\}\]', fields["prices"]), (
        "ровно одна позиция: у звёзд она единственная"
    )
    assert "provider_token" not in fields, "для звёзд токен провайдера не нужен"
    assert len(call.title) <= 32 and len(call.description) <= 255
    assert len(call.payload.encode()) <= 128


# ── меню команд и типы апдейтов ─────────────────────────────────────────────


async def test_the_command_menu_is_published_with_the_new_commands() -> None:
    session = RecordingSession()
    bot = fx.make_bot(session)

    await bot_app.publish_commands(bot)

    (call,) = session.sent(SetMyCommands)
    names = [command.command for command in call.commands]
    assert {"subscription", "terms", "support", "paysupport"} <= set(names)
    assert not extra_fields(call)


async def test_a_menu_that_could_not_be_published_does_not_stop_the_bot() -> None:
    session = RecordingSession()
    session.failures[SetMyCommands] = TelegramBadRequest(
        method=SetMyCommands(commands=[]), message="Bad Request: boom"
    )

    await bot_app.publish_commands(fx.make_bot(session))


def test_the_dispatcher_asks_telegram_for_every_update_type_the_payments_need() -> None:
    """`subscription` попадает в `allowed_updates` сам, как только есть хендлер (aiogram 3.30+)."""
    used = set(bot_app.build_dispatcher().resolve_used_update_types())

    assert {"pre_checkout_query", "message", "callback_query", "subscription"} <= used


def test_no_test_fake_accepts_any_arguments_for_a_bot_api_method() -> None:
    """Фейк с `**kwargs` пропустил `subscription_period` у `answer_invoice` — ровно эту ошибку."""
    offenders = [
        path.name
        for path in TESTS.glob("test_*.py")
        if path.name != Path(__file__).name
        and re.search(r"def answer_invoice\(", path.read_text(encoding="utf-8"))
    ]

    assert not offenders, offenders


class FakePolling:
    """Диспетчер без сети: `start_polling` ждёт остановки и помнит, что уже было отправлено."""

    def __init__(self, session: RecordingSession) -> None:
        self.session = session
        self.stopped = asyncio.Event()
        self.sent_before_listening: list[type] = []

    async def start_polling(self, bot: Bot, **_kwargs: object) -> None:
        self.sent_before_listening = [type(call) for call in self.session.calls]
        await self.stopped.wait()

    async def stop_polling(self) -> None:
        self.stopped.set()


async def test_the_process_publishes_the_command_menu_before_it_starts_listening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Без меню `/terms` и `/paysupport` есть, но о них никто не знает."""
    session = RecordingSession()
    polling = FakePolling(session)
    monkeypatch.setattr(bot_app, "Bot", lambda *_args, **_kwargs: fx.make_bot(session))
    monkeypatch.setattr(bot_app, "build_dispatcher", lambda: polling)
    stop = asyncio.Event()
    stop.set()

    await bot_app.run(stop)

    assert polling.sent_before_listening == [SetMyCommands]
