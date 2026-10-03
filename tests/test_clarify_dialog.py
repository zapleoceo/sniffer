"""Диалог с планировщиком: вопрос со счётчиками → ответ → честный счёт на выдаче.

Хранилище и журнал — подделки из соседних тестов; находки отбираются заглушкой
по паспорту так же, как отбирал бы поиск: неизвестное остаётся, названное
другое — уходит. Иначе тест про «счёт равен показанному» проверял бы константу.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest

from sniffer.bot.conversation import Conversation, Found, Reply
from sniffer.bot.keyboards import markup
from sniffer.config import get_settings
from sniffer.domain.clarify import ClarificationPlanner, Search
from sniffer.domain.dialogue import SHOW_ALL, SKIP, DialogueState, advance, replay
from sniffer.domain.facets import facets_from
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.simulation.stubs import MemoryStore
from sniffer.sources.base import RawItem
from tests.test_bot_dialog import CLIENT, FakeIntake, FakeJournal, Replies

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
CARD_LINK = "открыть оригинал"


def lot(n: int, brand: str, model: str | None) -> RawItem:
    attrs: dict[str, Any] = {"brand": brand}
    if model:
        attrs["model"] = model
    return RawItem(
        source="archive",
        external_id=str(n),
        url=f"https://t.me/c/{n}",
        title=f"{brand} {model or ''} {n}",
        price_raw="25",
        price_vnd=20_000_000,
        posted_at=NOW,
        raw={"listing_id": n, "attributes": attrs},
    )


def base() -> list[RawItem]:
    """40 лотов: Honda 24 (Lead 12, Vision 8, без модели 4), Yamaha 16 (Exciter 10, NVX 6)."""
    items: list[RawItem] = []
    plan = (
        ("honda", "lead", 12),
        ("honda", "vision", 8),
        ("honda", None, 4),
        ("yamaha", "exciter", 10),
        ("yamaha", "nvx", 6),
    )
    for brand, model, count in plan:
        items += [lot(len(items) + i, brand, model) for i in range(count)]
    return items


def select(items: list[RawItem], passport: Passport) -> list[RawItem]:
    """Отбор как у поиска: неизвестное остаётся, явно другое уходит."""
    want = passport.attributes
    return [
        item
        for item in items
        if all(
            key not in want or item.raw["attributes"].get(key) in (None, want[key])
            for key in ("brand", "model")
        )
    ]


def bike() -> Passport:
    return Passport(
        intent=Intent.BUY,
        category=Category.MOTORBIKE,
        city="nha_trang",
        budget=Budget(max=800, currency=Currency.USD),
        raw_query="ищу скутер",
    )


def talk(store: MemoryStore, items: list[RawItem], *, planner: bool = True) -> Conversation:
    async def find(passport: Passport) -> Found:
        return Found(items=select(items, passport))

    return Conversation(
        store,
        intake=lambda: FakeIntake(bike()),
        finder=find,
        recorder=FakeJournal(),
        planner=ClarificationPlanner() if planner else None,
    )


def cards(reply: Reply) -> int:
    return reply.text.count(CARD_LINK)


def header_numbers(text: str) -> tuple[int, int]:
    match = re.search(r"Подходит (\d+), показываю (\d+) лучших", text)
    assert match, text
    return int(match.group(1)), int(match.group(2))


async def test_a_big_selection_asks_with_counts_before_any_card() -> None:
    replies = Replies()
    await talk(MemoryStore(), base()).on_text(CLIENT, "ищу скутер", replies)

    question = replies.sent[-1].question
    assert question is not None
    assert replies.sent[-1].text.startswith("Подходит 40")
    assert all(cards(reply) == 0 for reply in replies.sent)
    labels = [option.label for option in question.buttons]
    assert labels[-1] == "Показать все 40"
    assert "Любой" in labels
    assert markup(replies.sent[-1]) is not None


async def test_answering_the_questions_ends_in_cards_with_an_honest_header() -> None:
    store, replies = MemoryStore(), Replies()
    talker = talk(store, base())
    await talker.on_text(CLIENT, "ищу скутер", replies)
    for _ in range(4):
        question = replies.sent[-1].question
        if question is None:
            break
        await talker.on_answer(CLIENT, question.code, question.options[0].value, replies)
    last = replies.sent[-1]
    assert last.question is None
    total, shown = header_numbers(last.text)
    assert shown == cards(last)
    assert total <= 10 or shown == get_settings().max_cards


async def test_the_count_in_the_header_is_the_count_of_what_is_found_and_shown() -> None:
    """Оракул: «Подходит N» равно длине отобранного, число карточек — показанному."""
    items = base()
    for brand in ("honda", "yamaha"):
        store, replies = MemoryStore(), Replies()
        talker = talk(store, items)
        await talker.on_text(CLIENT, "ищу скутер", replies)
        for _ in range(4):
            question = replies.sent[-1].question
            if question is None:
                break
            pick = brand if question.field == "attributes.brand" else question.options[0].value
            await talker.on_answer(CLIENT, question.code, pick, replies)
        final = replies.sent[-1]
        dialogue = await store.load(CLIENT)
        assert dialogue.passport is not None
        expected = select(items, dialogue.passport.passport)
        total, shown = header_numbers(final.text)
        assert total == len(expected) == facets_from(expected).total
        assert shown == cards(final) == min(len(expected), get_settings().max_cards)


async def test_show_all_stops_the_questions_and_is_remembered() -> None:
    store, replies = MemoryStore(), Replies()
    talker = talk(store, base())
    await talker.on_text(CLIENT, "ищу скутер", replies)
    question = replies.sent[-1].question
    assert question is not None
    await talker.on_answer(CLIENT, question.code, SHOW_ALL, replies)
    final = replies.sent[-1]
    assert final.question is None
    assert header_numbers(final.text)[0] == 40
    assert (await store.load(CLIENT)).state.show_all is True


async def test_any_marks_the_field_asked_and_moves_on() -> None:
    store, replies = MemoryStore(), Replies()
    talker = talk(store, base())
    await talker.on_text(CLIENT, "ищу скутер", replies)
    first = replies.sent[-1].question
    assert first is not None
    await talker.on_answer(CLIENT, first.code, SKIP, replies)
    second = replies.sent[-1].question
    assert second is None or second.field != first.field
    assert first.field in (await store.load(CLIENT)).state.asked


async def test_without_a_planner_nothing_changes() -> None:
    replies = Replies()
    await talk(MemoryStore(), base(), planner=False).on_text(CLIENT, "ищу скутер", replies)
    assert replies.sent[-1].question is None
    assert cards(replies.sent[-1]) == get_settings().max_cards


async def test_an_empty_answer_is_never_asked_about() -> None:
    replies = Replies()
    await talk(MemoryStore(), []).on_text(CLIENT, "ищу скутер", replies)
    assert all(reply.question is None for reply in replies.sent)


def test_show_all_lives_in_the_event_trail_and_is_sticky() -> None:
    state = advance(DialogueState(), "manual_edit", {"field": "x", "show_all": True})
    assert state.show_all
    assert advance(state, "user_message", {}).show_all
    assert advance(state, "question_asked", {"field": "y"}).show_all
    assert not advance(DialogueState(), "manual_edit", {"field": "x", "skipped": True}).show_all
    assert replay([]).show_all is False


@pytest.mark.parametrize("count", [0, 1, 10])
def test_the_target_is_ten(count: int) -> None:
    planner = ClarificationPlanner()
    items = base()[:count]
    assert planner.decide(bike(), facets_from(items), DialogueState()) == Search("small")


@pytest.mark.parametrize(
    ("mode", "enabled"),
    [
        ("listings", True),
        ("legacy", False),
        ("shadow", False),
        ("pilot", False),
        ("catalog", False),
    ],
)
def test_the_planner_runs_only_on_the_cheap_local_catalog(
    monkeypatch: pytest.MonkeyPatch, mode: str, enabled: bool
) -> None:
    """На живом поиске каждый шаг сужения платил бы моделью и обходом источников."""
    from types import SimpleNamespace

    from sniffer.bot.handlers import search as handler

    monkeypatch.setattr(handler, "get_settings", lambda: SimpleNamespace(catalog_mode=mode))
    assert isinstance(handler._planner(), ClarificationPlanner) is enabled
