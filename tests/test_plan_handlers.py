"""Хендлеры лимита: `/start`, `/plan`, неизвестная команда, кнопка «Подписка».

Настоящие функции хендлеров на подставных сообщениях: Telegram и база не нужны.
`/plan` читает журнал через `QuotaService` над `MemoryLedger`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from aiogram.types import Message

from sniffer.bot import wording, wording_plan
from sniffer.bot.commands import looks_like_command
from sniffer.bot.handlers import search as handler
from sniffer.bot.keyboards import PlanCallback, RequestsCallback, markup
from sniffer.bot.presenter import Reply
from sniffer.bot.quota import Account, QuotaService
from sniffer.bot.store import Client
from sniffer.simulation.ledger import MemoryLedger
from tests.quota_support import Clock

T0 = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)


pytestmark = pytest.mark.usefixtures("sales_on")


class FakeUser:
    def __init__(self, user_id: int = 42) -> None:
        self.id = user_id
        self.username = "dima"


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.from_user = FakeUser()
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs: Any) -> None:
        self.answers.append(text)


class FakeCallback:
    def __init__(self, message: FakeMessage) -> None:
        self.message = message
        self.from_user = message.from_user
        self.answered = False

    async def answer(self, **_kwargs: Any) -> None:
        self.answered = True


class Talker:
    def __init__(self) -> None:
        self.searched: list[str] = []

    async def on_text(self, _client: Client, text: str, _send: Any) -> None:
        self.searched.append(text)


# ── приветствие ─────────────────────────────────────────────────────────────


async def test_start_answers_with_the_greeting_that_states_the_limit() -> None:
    message = FakeMessage("/start")

    await handler.start(cast(Message, message))

    assert message.answers == [wording.GREETING]
    assert handler.GREETING is wording.GREETING, "имя осталось в хендлере: на него ссылаются тесты"


# ── неизвестная команда не становится поиском ───────────────────────────────


@pytest.mark.parametrize("text", ["/terms", "/paysupport@RecVNbot", "/foo bar", "/start2"])
async def test_an_unknown_command_is_answered_and_never_becomes_a_search_phrase(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    talker = Talker()
    monkeypatch.setattr(handler, "conversation", lambda: talker)
    message = FakeMessage(text)

    await handler.search(cast(Message, message))

    assert message.answers == [wording_plan.UNKNOWN_COMMAND]
    assert talker.searched == []


@pytest.mark.parametrize("text", ["ищу скутер", "/ 5 комнат", "/кв 2 спальни", "цена /мес"])
async def test_ordinary_text_still_reaches_the_conversation(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    talker = Talker()
    monkeypatch.setattr(handler, "conversation", lambda: talker)
    message = FakeMessage(text)

    await handler.search(cast(Message, message))

    assert talker.searched == [text] and message.answers == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/terms", True),
        ("/terms@RecVNbot", True),
        ("/new скутер", True),
        ("/a_1 x", True),
        (" /plan", False),
        ("/", False),
        ("//x", False),
        ("/ 5 комнат", False),
        ("ищу /terms", False),
    ],
)
def test_what_looks_like_a_command(text: str, expected: bool) -> None:
    assert looks_like_command(text) is expected


# ── /plan ───────────────────────────────────────────────────────────────────


async def test_plan_reads_the_standing_and_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = MemoryLedger()
    quota = QuotaService(ledger, clock=Clock(T0))
    who = Account(user_id=1, tg_user_id=42)
    await quota.confirm(await quota.admit(who, [1, 2, 3, 4, 5]))

    async def account_of(_client: Client) -> Account:
        return who

    monkeypatch.setattr(handler, "quota", lambda: quota)
    monkeypatch.setattr(handler, "account_of", account_of)
    message = FakeMessage("/plan")
    before = len(ledger.rows(1))

    await handler.plan(cast(Message, message))

    assert message.answers == [
        wording_plan.plan_text(await quota.standing(who), selling=True),
    ]
    assert "использовано 5 из 10" in message.answers[0] and "17 ноября" in message.answers[0]
    assert len(ledger.rows(1)) == before


# ── кнопка «Подписка» ───────────────────────────────────────────────────────


async def test_the_subscription_button_opens_the_subscription_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Кнопка «Подписка» — это `/subscription`: экран с цифрами, согласие, ссылка. Не заглушка."""
    monkeypatch.setattr(handler, "Message", FakeMessage)
    shown: list[tuple[object, object, int]] = []

    async def show(message: object, bot: object, tg_user_id: int) -> None:
        shown.append((message, bot, tg_user_id))

    monkeypatch.setattr(handler, "show_confirmation", show)
    message = FakeMessage("")
    callback = FakeCallback(message)
    bot = object()

    await handler.plan_action(cast(Any, callback), PlanCallback(action="subscribe"), cast(Any, bot))

    assert callback.answered and message.answers == []
    assert shown == [(message, bot, callback.from_user.id)]


async def test_a_foreign_plan_action_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "Message", FakeMessage)
    message = FakeMessage("")

    await handler.plan_action(
        cast(Any, FakeCallback(message)), PlanCallback(action="refund"), cast(Any, object())
    )

    assert message.answers == []


def test_the_offer_carries_the_price_on_the_button_and_a_way_out_without_paying() -> None:
    keyboard = markup(Reply("предложение", offer_plan=True))

    assert keyboard is not None
    rows = keyboard.inline_keyboard
    assert rows[0][0].text == wording_plan.SUBSCRIBE_LABEL and "10 ⭐" in rows[0][0].text
    assert rows[0][0].callback_data == PlanCallback(action="subscribe").pack()
    assert rows[1][0].callback_data == RequestsCallback(action="list").pack()


def test_a_reply_without_any_buttons_has_no_keyboard() -> None:
    assert markup(Reply("короткий ответ")) is None


def test_the_plan_button_fits_the_telegram_callback_limit() -> None:
    assert len(PlanCallback(action="subscribe").pack().encode()) <= 64


# ── продажа выключена: «Подписка» и /plan не продают ─────────────────────────


async def test_with_sales_off_the_subscription_button_only_says_it_is_coming(
    monkeypatch: pytest.MonkeyPatch, sales_off: None
) -> None:
    monkeypatch.setattr(handler, "Message", FakeMessage)
    shown: list[int] = []

    async def show(message: object, bot: object, tg_user_id: int) -> None:
        shown.append(tg_user_id)

    monkeypatch.setattr(handler, "show_confirmation", show)
    message = FakeMessage("")

    await handler.plan_action(
        cast(Any, FakeCallback(message)), PlanCallback(action="subscribe"), cast(Any, object())
    )

    assert message.answers == ["Расширенный тариф скоро."] and shown == []


async def test_with_sales_off_the_plan_command_has_no_subscription_pitch(
    monkeypatch: pytest.MonkeyPatch, sales_off: None
) -> None:
    quota = QuotaService(MemoryLedger(), clock=Clock(T0))
    who = Account(user_id=1, tg_user_id=42)
    await quota.confirm(await quota.admit(who, [1, 2, 3, 4, 5]))

    async def account_of(_client: Client) -> Account:
        return who

    monkeypatch.setattr(handler, "quota", lambda: quota)
    monkeypatch.setattr(handler, "account_of", account_of)
    message = FakeMessage("/plan")

    await handler.plan(cast(Message, message))

    (text,) = message.answers
    assert "использовано 5 из 10" in text and "17 ноября" in text
    assert "подписк" not in text.lower() and "⭐" not in text
