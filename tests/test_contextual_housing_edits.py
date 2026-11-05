"""A contextual housing edit keeps one root and all untouched search criteria."""

from __future__ import annotations

from sniffer.bot.conversation import Conversation, Found
from sniffer.bot.store import Client, Dialogue
from sniffer.config import Settings
from sniffer.db import models
from sniffer.db.mappers import passport_values
from sniffer.db.repositories.passports import PassportRepository
from sniffer.domain.passport import Category
from sniffer.search import intake as intake_module
from sniffer.search.intake import QueryIntake, intake_schema
from sniffer.search.intake_rules import parse_query
from sniffer.simulation.stubs import MemoryStore, SilentJournal

BASE = "Дом, аренда, север Нячанга, минимум 2 комнаты, от 7 до 15 млн VND в месяц"
FULL = "Хочу дом на севере Нячанга, минимум две комнаты, от 8 до 14 млн VND в месяц"
PARTIAL = "Верхнюю границу подними до 15 млн, остальное оставь"
REMOVE = "Нижнюю границу убери, остальное не меняй"
CLIENT = Client(tg_user_id=42, username="test")


async def _nothing(_passport: object) -> Found:
    return Found(items=[])


async def _reply(_reply: object) -> None:
    return None


def complete(coroutine):
    """Run a fake I/O coroutine without the broken Windows event loop."""
    try:
        coroutine.send(None)
    except StopIteration as finished:
        return finished.value
    raise AssertionError("The isolated fake unexpectedly required real asynchronous I/O")


def test_full_partial_remove_repeat_restore_one_root(monkeypatch) -> None:
    monkeypatch.setattr(
        intake_module,
        "get_settings",
        lambda: Settings(broker_project_key="", default_city="nha_trang"),
    )
    store = MemoryStore()
    original = parse_query(BASE, default_city="")
    complete(store.start(Dialogue(user_id=1), original))
    talker = Conversation(store, intake=QueryIntake, finder=_nothing, recorder=SilentJournal())

    complete(talker.on_text(CLIENT, FULL, _reply))
    full = complete(store.load(CLIENT))
    assert full.passport is not None
    root = full.passport.root
    passport = full.passport.passport
    assert passport.category is Category.HOUSE
    assert passport.attributes["rooms"] == 2
    assert passport.attributes["rooms_min"] is True
    assert passport.districts == original.districts
    assert (passport.budget.min, passport.budget.max) == (8_000_000, 14_000_000)

    complete(talker.on_text(CLIENT, PARTIAL, _reply))
    partial = complete(store.load(CLIENT))
    assert partial.passport is not None
    assert partial.passport.root == root
    assert (partial.passport.passport.budget.min, partial.passport.passport.budget.max) == (
        8_000_000,
        15_000_000,
    )

    complete(talker.on_text(CLIENT, REMOVE, _reply))
    removed = complete(store.load(CLIENT))
    assert removed.passport is not None
    assert removed.passport.root == root
    assert removed.passport.passport.budget.min is None
    assert removed.passport.passport.budget.max == 15_000_000
    assert store.events[-1].payload["removed"] == ["budget_min"]
    assert complete(store.load(CLIENT)).passport.passport.budget.min is None

    complete(store.select(removed, root, editing=True))
    complete(talker.on_text(CLIENT, BASE, _reply))
    restored = complete(store.load(CLIENT))
    assert restored.passport is not None
    assert restored.passport.root == root
    assert restored.passport.passport.model_dump() == original.model_dump()
    assert {row.root for row in store.rows} == {root}
    assert store.monitoring == {}


def test_removed_floor_cannot_recover_from_old_raw_query() -> None:
    original = parse_query(BASE, default_city="")
    row = models.Passport(id=71, user_id=1, version=4, root_id=71, **passport_values(original))
    row.budget = {**row.budget, "min": None}

    class Session:
        async def scalar(self, _query: object) -> int:
            return 1  # The same root has a manual_edit removed=['budget_min'] event.

    reloaded = complete(PassportRepository(Session()).map_stored(row))  # type: ignore[arg-type]
    assert reloaded.passport.budget.min is None
    assert reloaded.passport.budget.max == 15_000_000


def test_written_bedrooms_are_not_total_rooms() -> None:
    passport = parse_query("Дом на севере Нячанга, две спальни, до 15 млн")
    assert passport.category is Category.HOUSE
    assert passport.attributes["bedrooms"] == 2
    assert "rooms" not in passport.attributes


def test_mock_model_sees_context_and_can_request_explicit_floor_removal(monkeypatch) -> None:
    monkeypatch.setattr(
        intake_module,
        "get_settings",
        lambda: Settings(broker_project_key="", default_city="nha_trang"),
    )

    class Broker:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def structured(self, prompt: str, **_kwargs: object) -> dict[str, str]:
            self.prompts.append(prompt)
            return {
                "budget_min_action": "remove",
                "budget_min_evidence": "без минимальной цены",
            }

    broker = Broker()
    intake = QueryIntake(broker=broker)
    current = parse_query(BASE, default_city="")
    _, remove = complete(intake.parse_edit("Сделай бюджет без минимальной цены", current))
    assert remove
    assert "Текущий поиск" in broker.prompts[0]
    assert "7000000" in broker.prompts[0]
    assert "budget_min_action" in intake_schema()["required"]
    assert "budget_min_evidence" in intake_schema()["required"]
    assert "total_rooms_evidence" in intake_schema()["required"]
