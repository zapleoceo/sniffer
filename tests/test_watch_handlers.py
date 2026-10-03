"""Хендлеры панели и карточки фильтра без сети и базы: флоу подменены, Telegram — запись вызовов."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import CallbackQuery, Chat, Message, User

from sniffer.bot import filter_card as card
from sniffer.bot import filter_flow, watch_flow
from sniffer.bot import watch_button as button
from sniffer.bot import watch_panel as panel
from sniffer.bot.handlers import watch as handlers
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.domain.passport_edit import Change
from sniffer.domain.records import QueryOverview

PASSPORT = Passport(
    intent=Intent.BUY,
    category=Category.MOTORBIKE,
    city="nha_trang",
    budget=Budget(max=15_000_000, currency=Currency.VND),
    attributes={"transmission": "automatic"},
    raw_query="x",
)
VIEW = card.CardView(root=5, version=2, passport=PASSPORT, monitoring="active")


class Wire:
    """Что бот отправил в Telegram: правки сообщения и новые сообщения."""

    def __init__(self) -> None:
        self.edited: list[tuple[str, Any]] = []
        self.answered: list[str] = []


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Wire:
    wire = Wire()

    async def edit_text(self: Message, text: str, reply_markup: Any = None, **_: Any) -> None:
        wire.edited.append((text, reply_markup))

    async def answer(self: Message, text: str, reply_markup: Any = None, **_: Any) -> None:
        wire.answered.append(text)

    async def callback_answer(self: CallbackQuery, *_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(Message, "edit_text", edit_text)
    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setattr(CallbackQuery, "answer", callback_answer)
    return wire


def callback(data: str) -> CallbackQuery:
    message = Message.model_construct(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=9, type="private")
    )
    return CallbackQuery.model_construct(
        id="c", from_user=User(id=9, is_bot=False, first_name="x"), message=message, data=data
    )


async def press(**fields: Any) -> None:
    data = card.FilterCallback(root=5, v=2, **fields)
    await handlers.on_filter(callback(data.pack()), data)


@pytest.fixture
def flows(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int, list[Change]]]:
    edits: list[tuple[int, int, list[Change]]] = []

    async def open_card(client: Any, root: int) -> card.CardView | None:
        return VIEW if root == 5 else None

    async def edit(client: Any, root: int, base: int, changes: Any) -> filter_flow.Outcome:
        edits.append((root, base, list(changes)))
        return filter_flow.Outcome(VIEW, None)

    monkeypatch.setattr(filter_flow, "open_card", open_card)
    monkeypatch.setattr(filter_flow, "edit", edit)
    return edits


async def test_clear_removes_the_field_on_the_version_the_person_saw(
    wire: Wire, flows: list[Any]
) -> None:
    await press(f="transmission", a=card.CLEAR)

    assert flows == [(5, 2, [Change("transmission")])]
    assert wire.edited and "Бюджет до" in wire.edited[-1][0]


async def test_a_flag_button_sets_a_boolean(wire: Wire, flows: list[Any]) -> None:
    await press(f="pool", a=card.SET, o="true")
    assert flows[0][2] == [Change("pool", True)]


async def test_a_choice_button_sets_the_option(wire: Wire, flows: list[Any]) -> None:
    await press(f="transmission", a=card.SET, o="manual")
    assert flows[0][2] == [Change("transmission", "manual")]


async def test_a_stale_note_reaches_the_person(
    wire: Wire, flows: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def edit(client: Any, root: int, base: int, changes: Any) -> filter_flow.Outcome:
        return filter_flow.Outcome(VIEW, card.STALE)

    monkeypatch.setattr(filter_flow, "edit", edit)
    await press(f="transmission", a=card.CLEAR)
    assert card.STALE in wire.edited[-1][0]


async def test_an_unknown_search_is_answered_not_edited(wire: Wire, flows: list[Any]) -> None:
    data = card.FilterCallback(root=404, v=1, f="brand", a=card.CLEAR)
    await handlers.on_filter(callback(data.pack()), data)
    assert wire.answered and not wire.edited and flows == []


async def test_a_text_field_asks_for_words_and_turns_editing_on(
    wire: Wire, flows: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[int] = []

    async def start_text_edit(client: Any, root: int) -> bool:
        started.append(root)
        return True

    monkeypatch.setattr(filter_flow, "start_text_edit", start_text_edit)
    await press(f="budget_max", a=card.OPEN)

    assert started == [5] and flows == []
    assert "напишите" in wire.answered[-1]


async def test_a_choice_field_opens_its_buttons_instead(wire: Wire, flows: list[Any]) -> None:
    await press(f="transmission", a=card.OPEN)
    assert wire.edited and "выберите" in wire.edited[-1][0]


async def test_pressing_the_same_button_twice_is_not_an_error(
    wire: Wire, flows: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def not_modified(self: Message, *a: Any, **k: Any) -> None:
        raise TelegramBadRequest(
            EditMessageText(text="x"), "Bad Request: message is not modified: same content"
        )

    monkeypatch.setattr(Message, "edit_text", not_modified)
    await press(f="", a=card.BACK)  # без исключения


async def test_a_real_bad_request_is_not_swallowed(
    wire: Wire, flows: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: Message, *a: Any, **k: Any) -> None:
        raise TelegramBadRequest(
            EditMessageText(text="x"), "Bad Request: message to edit not found"
        )

    monkeypatch.setattr(Message, "edit_text", broken)
    with pytest.raises(TelegramBadRequest):
        await press(f="", a=card.BACK)


async def test_a_new_search_over_the_limit_is_refused_with_the_numbers(
    wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = [QueryOverview(root=1, passport=PASSPORT)]
    full = panel.PanelView(items, paid_slots=0, bound_slots=0, used=1, cap=1)

    async def can_open_new(client: Any) -> tuple[bool, panel.PanelView]:
        return False, full

    monkeypatch.setattr(watch_flow, "can_open_new", can_open_new)
    data = button.WatchCallback(a=panel.NEW)
    await handlers.on_watch(callback(data.pack()), data)

    assert wire.answered[-1].startswith("У вас уже 1 поиск — поставьте на паузу или удалите один")
    assert "до 10" in wire.answered[-1]  # бесплатному говорим, что платный предел выше


async def test_a_paid_account_over_the_limit_is_not_pitched_a_subscription(
    wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    full = panel.PanelView([], paid_slots=1, bound_slots=1, used=10, cap=10)

    async def can_open_new(client: Any) -> tuple[bool, panel.PanelView]:
        return False, full

    monkeypatch.setattr(watch_flow, "can_open_new", can_open_new)
    data = button.WatchCallback(a=panel.NEW)
    await handlers.on_watch(callback(data.pack()), data)
    assert wire.answered[-1].startswith("У вас уже 10 поисков")
    assert "подписку" not in wire.answered[-1]
