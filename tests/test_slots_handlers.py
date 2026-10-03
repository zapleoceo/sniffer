"""Кнопки слежения: «Следить» включает, зовёт к подписке или предлагает перенос слота.

Настоящие апдейты идут через Dispatcher (как в `test_billing_handlers.py`); слоты и список
поисков — подделки: решения о слоте проверяет `test_slots_domain.py`, а SQL — живая база.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import InlineKeyboardMarkup

from sniffer.bot import billing_wording, query_menu
from sniffer.bot import slot_wording as words
from sniffer.bot.handlers import slots as slots_handler
from sniffer.bot.handlers.slots import MOVE, SlotCallback
from sniffer.bot.keyboards import SubscribeCallback
from sniffer.domain.records import QueryOverview
from sniffer.domain.slots import Monitor, Outcome
from tests import billing_support as fx
from tests.billing_support import CLIENT
from tests.test_billing_handlers import Wired, wire
from tests.thread_support import bike

ROOT = 7


pytestmark = pytest.mark.usefixtures("sales_on")


class FakeSlotService:
    """`DbSlots` без базы: исход «Следить» задаёт тест, вызовы записываются."""

    def __init__(self) -> None:
        self.outcome = Outcome.ENABLE
        self.held: list[Monitor] = []
        self.moved = True
        self.enabled: list[tuple[int, int]] = []
        self.moves: list[tuple[int, int, int]] = []

    async def enable(self, tg_user_id: int, root: int, now: datetime) -> Outcome:
        self.enabled.append((tg_user_id, root))
        return self.outcome

    async def holders(self, tg_user_id: int, now: datetime) -> list[Monitor]:
        return self.held

    async def move(self, tg_user_id: int, *, to_root: int, from_root: int, now: datetime) -> bool:
        self.moves.append((tg_user_id, to_root, from_root))
        return self.moved


@pytest.fixture
def slots(monkeypatch: pytest.MonkeyPatch) -> FakeSlotService:
    fake = FakeSlotService()
    monkeypatch.setattr(slots_handler, "slots_service", lambda: fake)
    return fake


@pytest.fixture
def known_searches(monkeypatch: pytest.MonkeyPatch) -> dict[int, QueryOverview]:
    """Какие поиски «есть» у клиента: чужой или несуществующий корень в списке не значится."""
    mine = {ROOT: QueryOverview(root=ROOT, passport=bike())}

    async def get_one(client: object, root: int) -> QueryOverview | None:
        return mine.get(root)

    monkeypatch.setattr(query_menu, "get_one", get_one)
    return mine


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Iterator[Wired]:
    yield from wire(monkeypatch)


def press(root: int = ROOT) -> Any:
    return fx.callback(SubscribeCallback(root=root).pack())


def keyboard_of(call: SendMessage) -> InlineKeyboardMarkup:
    assert isinstance(call.reply_markup, InlineKeyboardMarkup)
    return call.reply_markup


async def test_follow_with_a_free_slot_enables_tracking(
    wired: Wired, slots: FakeSlotService, known_searches: dict[int, QueryOverview]
) -> None:
    await wired.feed(press())

    assert slots.enabled == [(CLIENT, ROOT)]
    assert [m.text for m in wired.messages()] == [words.ENABLED]


async def test_follow_on_an_already_tracked_search_says_so(
    wired: Wired, slots: FakeSlotService, known_searches: dict[int, QueryOverview]
) -> None:
    slots.outcome = Outcome.ALREADY_ON

    await wired.feed(press())

    assert [m.text for m in wired.messages()] == [words.ALREADY_ON]


async def test_follow_without_a_subscription_leads_to_the_priced_screen_not_to_an_invoice(
    wired: Wired, slots: FakeSlotService, known_searches: dict[int, QueryOverview]
) -> None:
    slots.outcome = Outcome.NEEDS_SUBSCRIPTION

    await wired.feed(press())

    texts = [m.text for m in wired.messages()]
    assert texts == [words.NEEDS_SUBSCRIPTION, billing_wording.confirmation(0)]
    assert wired.session.calls and not any(
        type(call).__name__ in {"CreateInvoiceLink", "SendInvoice"} for call in wired.session.calls
    ), "до согласия со счётом ничего не делается"


async def test_follow_with_every_slot_taken_offers_a_move_and_a_purchase(
    wired: Wired, slots: FakeSlotService, known_searches: dict[int, QueryOverview]
) -> None:
    slots.outcome = Outcome.NO_FREE_SLOT
    known_searches[3] = QueryOverview(root=3, passport=bike())
    slots.held = [Monitor(id=1, root=3, priority=0, expires_at=None)]

    await wired.feed(press())

    (message,) = wired.messages()
    rows = keyboard_of(message).inline_keyboard
    move = SlotCallback.unpack(rows[0][0].callback_data or "")
    buy = SlotCallback.unpack(rows[-1][0].callback_data or "")
    assert (move.action, move.root, move.source) == (MOVE, ROOT, 3)
    assert buy.action == "buy" and rows[-1][0].text == words.ADD_SLOT_LABEL


async def test_a_foreign_or_missing_search_is_refused_before_any_slot_is_touched(
    wired: Wired, slots: FakeSlotService, known_searches: dict[int, QueryOverview]
) -> None:
    await wired.feed(press(root=999))
    await wired.feed(press(root=0))

    assert slots.enabled == []
    assert [m.text for m in wired.messages()] == [words.NOT_YOURS, words.NOT_YOURS]


async def test_the_move_button_moves_the_slot_and_says_so(
    wired: Wired, slots: FakeSlotService, known_searches: dict[int, QueryOverview]
) -> None:
    await wired.feed(fx.callback(SlotCallback(action=MOVE, root=ROOT, source=3).pack()))

    assert slots.moves == [(CLIENT, ROOT, 3)]
    assert [m.text for m in wired.messages()] == [words.MOVED]


async def test_a_move_that_no_longer_applies_is_not_reported_as_done(
    wired: Wired, slots: FakeSlotService
) -> None:
    slots.moved = False

    await wired.feed(fx.callback(SlotCallback(action=MOVE, root=ROOT, source=3).pack()))

    assert [m.text for m in wired.messages()] == [words.MOVE_FAILED]


async def test_the_buy_button_opens_the_subscription_screen(
    wired: Wired, slots: FakeSlotService
) -> None:
    await wired.feed(fx.callback(SlotCallback(action="buy", root=ROOT).pack()))

    assert [m.text for m in wired.messages()] == [billing_wording.confirmation(0)]


@pytest.mark.parametrize(
    "data",
    [SubscribeCallback(root=ROOT).pack(), SlotCallback(action=MOVE, root=ROOT, source=3).pack()],
    ids=["follow", "move"],
)
async def test_a_stale_button_says_so_instead_of_silence(
    wired: Wired, slots: FakeSlotService, data: str
) -> None:
    await wired.feed(fx.stale_callback(data))

    (answer,) = wired.session.sent(AnswerCallbackQuery)
    assert answer.text == words.STALE and answer.show_alert is True
    assert wired.messages() == [] and slots.enabled == [] and slots.moves == []


async def test_an_unknown_slot_action_is_a_stale_button_not_a_crash(
    wired: Wired, slots: FakeSlotService
) -> None:
    await wired.feed(fx.callback(SlotCallback(action="что-то", root=ROOT).pack()))

    assert [m.text for m in wired.messages()] == [words.STALE]


def test_the_follow_label_carries_no_price() -> None:
    """У подписчика со свободным слотом «Следить» бесплатно: цена на кнопке врала бы."""
    assert "⭐" not in words.FOLLOW_LABEL


# ── продажа выключена: путь к покупке заменён одной строкой ─────────────────


@pytest.mark.parametrize("outcome", [Outcome.NEEDS_SUBSCRIPTION, Outcome.NO_FREE_SLOT])
async def test_with_sales_off_follow_never_leads_to_a_purchase(
    wired: Wired,
    slots: FakeSlotService,
    known_searches: dict[int, QueryOverview],
    sales_off: None,
    outcome: Outcome,
) -> None:
    slots.outcome = outcome

    await wired.feed(press())

    assert [m.text for m in wired.messages()] == [billing_wording.SOON]
    assert billing_wording.SOON == "Расширенный тариф скоро."


async def test_with_sales_off_an_already_available_slot_still_enables_tracking(
    wired: Wired,
    slots: FakeSlotService,
    known_searches: dict[int, QueryOverview],
    sales_off: None,
) -> None:
    await wired.feed(press())

    assert [m.text for m in wired.messages()] == [words.ENABLED]
