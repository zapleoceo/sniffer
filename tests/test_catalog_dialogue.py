"""Rollout isolation and trusted per-request identity, without network/model calls."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from sniffer.bot.catalog_finder import CatalogFinder
from sniffer.bot.conversation import Conversation, Found
from sniffer.bot.store import Dialogue
from sniffer.config import Settings
from sniffer.domain.passport import Passport
from sniffer.domain.records import StoredPassport
from sniffer.search.intake_rules import parse_query
from sniffer.sources.base import RawItem
from tests.test_bot_dialog import CLIENT, FakeJournal, MemoryStore, Replies


def scope() -> Dialogue:
    return Dialogue(
        user_id=7,
        passport=StoredPassport(
            id=30,
            user_id=7,
            root_id=12,
            version=3,
            passport=parse_query("скутер в нячанге до 500 долларов"),
        ),
    )


@pytest.mark.parametrize(
    ("mode", "pilots", "enabled"),
    [
        ("legacy", (), False),
        ("pilot", (), False),
        ("pilot", (8,), False),
        ("pilot", (7,), True),
        ("catalog", (), True),
    ],
)
async def test_mode_routes_only_trusted_db_user(
    mode: str, pilots: tuple[int, ...], enabled: bool
) -> None:
    settings = Settings.model_validate({"catalog_mode": mode, "catalog_pilot_user_ids": pilots})
    legacy = AsyncMock(return_value=Found([]))
    catalog = AsyncMock(return_value=Found([], status="Сбор запланирован"))
    result = await CatalogFinder(legacy=legacy, catalog=catalog, settings=lambda: settings)(scope())
    assert legacy.await_count == int(not enabled)
    assert catalog.await_count == int(enabled)
    if enabled:
        catalog.assert_awaited_once_with(7, 12, 3, allow_collection=True)
        assert result.status == "Сбор запланирован"


async def test_shadow_never_enqueues_or_replaces_legacy_cards() -> None:
    original = Found([RawItem("legacy", "1", "https://example.com/1")])
    catalog = AsyncMock(return_value=Found([], status="do not send"))
    finder = CatalogFinder(
        legacy=AsyncMock(return_value=original),
        catalog=catalog,
        settings=lambda: Settings(catalog_mode="shadow"),
    )
    assert await finder(scope()) is original
    catalog.assert_awaited_once_with(7, 12, 3, allow_collection=False)


async def test_shadow_failure_does_not_change_result_and_cancellation_propagates() -> None:
    original = Found([])
    catalog = AsyncMock(side_effect=RuntimeError("secret"))
    finder = CatalogFinder(
        legacy=AsyncMock(return_value=original),
        catalog=catalog,
        settings=lambda: Settings(catalog_mode="shadow"),
    )
    assert await finder(scope()) is original
    catalog.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await finder(scope())


async def test_catalog_failure_never_falls_back_to_live_writes() -> None:
    legacy = AsyncMock()
    finder = CatalogFinder(
        legacy=legacy,
        catalog=AsyncMock(side_effect=RuntimeError("failed")),
        settings=lambda: Settings(catalog_mode="catalog"),
    )
    with pytest.raises(RuntimeError):
        await finder(scope())
    legacy.assert_not_awaited()


async def test_shadow_deadline_cancels_catalog_and_preserves_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = Found([])
    cancelled = False
    timeout = asyncio.timeout

    async def slow(*args: object, **kwargs: object) -> Found:
        nonlocal cancelled
        try:
            await asyncio.sleep(10)
        finally:
            cancelled = True
        return Found([])

    monkeypatch.setattr("sniffer.bot.catalog_finder.asyncio.timeout", lambda _: timeout(0.001))
    finder = CatalogFinder(
        legacy=AsyncMock(return_value=original),
        catalog=slow,
        settings=lambda: Settings(catalog_mode="shadow"),
    )
    assert await finder(scope()) is original
    assert cancelled


@pytest.mark.parametrize("dialogue", [Dialogue(7), replace(scope(), user_id=8)])
async def test_invalid_scope_has_zero_calls(dialogue: Dialogue) -> None:
    legacy, catalog = AsyncMock(), AsyncMock()
    with pytest.raises(ValueError):
        await CatalogFinder(legacy=legacy, catalog=catalog)(dialogue)
    legacy.assert_not_awaited()
    catalog.assert_not_awaited()


def test_default_and_invalid_settings() -> None:
    assert Settings.model_construct().catalog_mode == "legacy"
    with pytest.raises(ValidationError):
        Settings.model_validate({"catalog_mode": "autodetect"})


async def test_scoped_dialogue_asks_only_for_category_and_reports_empty_status() -> None:
    """search-first (владелец, 04.09.2026): единственный блокирующий вопрос —
    категория. Путь через `scoped_finder` (каталожный режим) обязан вести себя
    так же, как путь через обычный `finder` — вопросы про город и бюджет из
    воронки ушли отовсюду разом, а не только с легаси-пути.
    """

    class Parser:
        async def parse(self, text: str) -> Passport:
            return parse_query(text)

    store = MemoryStore()
    finder = AsyncMock(return_value=Found([], status="Данных пока нет. Сбор запланирован."))
    legacy = AsyncMock()
    talk = Conversation(
        store, intake=Parser, finder=legacy, scoped_finder=finder, recorder=FakeJournal()
    )
    replies = Replies()
    await talk.on_text(CLIENT, "ищу что-нибудь", replies)
    assert replies.sent[-1].question is not None
    assert replies.sent[-1].question.field == "category"
    finder.assert_not_awaited()

    await talk.on_answer(CLIENT, "cat", "scooter", replies)
    assert finder.await_count == 1
    legacy.assert_not_awaited()
    assert replies.texts[-1].startswith("Данных пока нет. Сбор запланирован.")
    current = await store.load(CLIENT)
    assert finder.await_args is not None
    assert finder.await_args.args[0] == current
    assert current.state.asked == ("category",)


async def test_selected_old_request_and_revised_version_reach_catalog() -> None:
    from tests.test_bot_dialog import RulesIntake

    store = MemoryStore()
    catalog = AsyncMock(return_value=Found([]))
    finder = CatalogFinder(catalog=catalog, settings=lambda: Settings(catalog_mode="catalog"))
    talk = Conversation(store, intake=RulesIntake, scoped_finder=finder, recorder=FakeJournal())
    await talk.on_text(CLIENT, "скутер в нячанге до 500 долларов", Replies())
    first = await store.load(CLIENT)
    assert first.passport is not None
    await talk.on_text(CLIENT, "квартира в нячанге до 1000 долларов", Replies())
    second = await store.load(CLIENT)
    assert second.passport is not None and second.passport.root != first.passport.root
    await store.select(second, first.passport.root, editing=True)
    await talk.on_text(CLIENT, "до 300 долларов", Replies())
    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.root == first.passport.root
    assert current.passport.version > first.passport.version
    catalog.assert_awaited_with(
        first.user_id, first.passport.root, current.passport.version, allow_collection=True
    )
