"""Постоянная клавиатура: те же действия, что у команд, и подпись кнопки не становится поиском."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.types import Chat, Message, ReplyKeyboardMarkup, User

from sniffer.bot import wording
from sniffer.bot.billing_wording import COMMANDS
from sniffer.bot.handlers import billing, menu, search
from sniffer.bot.keyboards import main_menu


def text_message(text: str) -> Message:
    return Message.model_construct(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=9, type="private"),
        from_user=User(id=9, is_bot=False, first_name="x"),
        text=text,
    )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def spy(name: str) -> Any:
        async def handler(*args: Any, **kwargs: Any) -> None:
            seen.append(name)

        return handler

    for owner, name in [
        (search, "new_request"),
        (search, "requests"),
        (search, "plan"),
        (search, "help_command"),
        (billing, "subscription_command"),
    ]:
        monkeypatch.setattr(owner, name, spy(name))
    return seen


def test_the_keyboard_is_persistent_and_resizable_with_the_five_buttons() -> None:
    keyboard = main_menu()
    assert isinstance(keyboard, ReplyKeyboardMarkup)
    assert keyboard.is_persistent and keyboard.resize_keyboard
    texts = [b.text for row in keyboard.keyboard for b in row]
    assert texts == list(wording.MENU_BUTTONS) and len(set(texts)) == 5


@pytest.mark.parametrize(
    ("button", "action"),
    [
        (wording.BTN_NEW, "new_request"),
        (wording.BTN_REQUESTS, "requests"),
        (wording.BTN_PLAN, "plan"),
        (wording.BTN_SUBSCRIPTION, "subscription_command"),
        (wording.BTN_HELP, "help_command"),
    ],
)
async def test_each_button_calls_the_action_of_its_command(
    button: str, action: str, calls: list[str]
) -> None:
    message = text_message(button)
    for handler in menu.router.message.handlers:
        matched, _ = await handler.check(message)
        if matched:
            await handler.call(message, bot=None)
    assert calls == [action]


@pytest.mark.parametrize("text", ["подписка на скутер", "помощь", "🔍 Новый поиск скутер", "/new"])
async def test_only_the_exact_button_text_is_intercepted(text: str) -> None:
    message = text_message(text)
    for handler in menu.router.message.handlers:
        matched, _ = await handler.check(message)
        assert not matched


async def test_start_shows_the_keyboard_and_help_names_the_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, Any]] = []

    async def answer(self: Message, text: str, reply_markup: Any = None, **_: Any) -> None:
        sent.append((text, reply_markup))

    monkeypatch.setattr(Message, "answer", answer)
    await search.start(text_message("/start"))
    await search.help_command(text_message("/help"))

    assert isinstance(sent[0][1], ReplyKeyboardMarkup)
    assert all(button.split(" ", 1)[1] in sent[1][0] for button in wording.MENU_BUTTONS[:4])


def test_the_command_menu_has_the_same_items_as_the_keyboard() -> None:
    names = {name for name, _ in COMMANDS}
    assert {"new", "requests", "plan", "subscription", "help"} <= names


def test_menu_and_payment_routers_come_before_the_dialog_in_the_dispatcher() -> None:
    """Подпись кнопки — обычный текст: после диалога его `F.text` принял бы её за поиск."""
    from sniffer.bot import app as bot_app

    names = [router.name for router in bot_app.build_dispatcher().sub_routers]
    assert names.index("menu") < names.index("search")
    assert names.index("billing") < names.index("search")
    assert names.index("slots") < names.index("search")
