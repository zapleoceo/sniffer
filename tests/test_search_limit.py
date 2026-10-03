"""Предел поисков (бесплатно 1, платно 10) на ВСЕХ путях открытия поиска, и тема, и пауза.

Пути: кнопка «Новый поиск», `/new`, «➕» панели — ворота `search_gate`; новый поиск по обычным
словам — `Conversation._open`. Внутри темы `/new` открывает новую тему, а не взводит флаг.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest
from aiogram.types import Message

from sniffer.bot import search_gate, topics, watch_flow
from sniffer.bot import watch_panel as panel
from sniffer.bot.search_gate import Start
from sniffer.bot.store import Client
from sniffer.simulation.stubs import MemoryStore
from tests.test_topics import FakeBot, network_error
from tests.thread_support import CLIENT, Replies, talk

FULL = panel.PanelView([], paid_slots=0, bound_slots=0, used=1, cap=1)


class Limit:
    """Предел поисков: отказ, когда в хранилище уже есть `cap` поисков."""

    def __init__(self, store: MemoryStore, cap: int) -> None:
        self._store, self._cap = store, cap

    async def blocked(self, user_id: int) -> str | None:
        live = await self._store.live_threads(await self._store.load(CLIENT))
        return None if len(live) < self._cap else "ПРЕДЕЛ"


async def test_a_second_search_by_words_is_refused_at_the_free_limit() -> None:
    store = MemoryStore()
    talker = talk(store)
    talker._search_limit = Limit(store, 1)
    await talker.on_text(CLIENT, "ищу скутер в нячанге до 400 долларов", Replies())

    said = Replies()
    await talker.on_text(CLIENT, "сниму квартиру в нячанге", said)

    assert said.texts == ["ПРЕДЕЛ"]
    assert len(await store.live_threads(await store.load(CLIENT))) == 1


async def test_a_refused_slash_new_is_disarmed_so_the_next_message_is_not_refused_again() -> None:
    store = MemoryStore()
    talker = talk(store)
    talker._search_limit = Limit(store, 1)
    await talker.on_text(CLIENT, "ищу скутер в нячанге до 400 долларов", Replies())
    await talker.start_new(CLIENT)

    await talker.on_text(CLIENT, "сниму квартиру в нячанге", Replies())

    assert (await store.load(CLIENT)).starting_new is False


async def test_the_first_search_is_never_refused() -> None:
    store = MemoryStore()
    talker = talk(store)
    talker._search_limit = Limit(store, 1)
    said = Replies()

    await talker.on_text(CLIENT, "ищу скутер в нячанге до 400 долларов", said)

    assert "ПРЕДЕЛ" not in said.texts
    assert len(await store.live_threads(await store.load(CLIENT))) == 1


# ── ворота ──────────────────────────────────────────────────────────────────


class Chat:
    def __init__(self, bot: FakeBot | None = None) -> None:
        self.texts: list[str] = []
        self.markups: list[Any] = []
        self.bot = bot

    async def answer(self, text: str, reply_markup: Any = None, **_: Any) -> None:
        self.texts.append(text)
        self.markups.append(reply_markup)


class Talker:
    def __init__(self) -> None:
        self.armed = 0

    async def start_new(self, _client: Client) -> None:
        self.armed += 1


def _gate(chat: Chat, talker: Talker, client: Client, *, prefer_tab: bool) -> Any:
    return search_gate.start_new_search(
        cast(Message, chat), client, cast(Any, talker), prefer_tab=prefer_tab
    )


@pytest.fixture
def room(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"view": FULL, "allowed": False}

    async def can_open_new(_client: Client) -> tuple[bool, panel.PanelView | None]:
        return state["allowed"], state["view"]

    monkeypatch.setattr(watch_flow, "can_open_new", can_open_new)
    topics.set_active(False)
    yield state
    topics.set_active(False)


async def test_a_full_account_is_refused_with_the_count_and_a_panel_button(
    room: dict[str, Any],
) -> None:
    chat, talker = Chat(), Talker()

    started = await _gate(chat, talker, CLIENT, prefer_tab=False)

    assert started is Start.REFUSED and talker.armed == 0
    assert chat.texts == [panel.limit_text(FULL)]
    assert chat.texts[0].startswith("У вас уже 1 поиск — поставьте на паузу или удалите один")
    assert chat.markups[0] is not None


async def test_a_free_account_with_room_arms_the_flag_and_says_nothing_itself(
    room: dict[str, Any],
) -> None:
    room["allowed"] = True
    chat, talker = Chat(), Talker()

    assert await _gate(chat, talker, CLIENT, prefer_tab=False) is Start.ARMED
    assert talker.armed == 1 and chat.texts == []


async def test_inside_a_topic_slash_new_opens_a_new_topic_instead_of_arming(
    room: dict[str, Any],
) -> None:
    room["allowed"] = True
    topics.set_active(True)
    bot = FakeBot()
    chat, talker = Chat(bot), Talker()
    in_topic = Client(tg_user_id=9, thread_id=77)

    started = await _gate(chat, talker, in_topic, prefer_tab=True)

    assert started is Start.TAB and talker.armed == 0
    assert bot.created and "Новый поиск" in chat.texts[0]


async def test_a_failed_topic_falls_back_to_the_chat_flag(room: dict[str, Any]) -> None:
    room["allowed"] = True
    topics.set_active(True)
    bot = FakeBot(error=network_error())
    chat, talker = Chat(bot), Talker()

    started = await _gate(chat, talker, Client(tg_user_id=9, thread_id=1), prefer_tab=True)

    assert started is Start.ARMED and talker.armed == 1
