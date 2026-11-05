"""A contextual housing edit keeps one root and all untouched search criteria."""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any, cast

import pytest

from sniffer.bot.conversation import Conversation, Found, _edited
from sniffer.bot.store import Client, Dialogue
from sniffer.config import Settings
from sniffer.db import models
from sniffer.db.mappers import passport_values
from sniffer.db.repositories.passports import PassportRepository
from sniffer.domain.field_spec import spec_by_key
from sniffer.domain.passport import Category
from sniffer.domain.passport_edit import Change
from sniffer.search import intake as intake_module
from sniffer.search.intake import QueryIntake, intake_schema
from sniffer.search.intake_rules import parse_query
from sniffer.search.refinements import removes_budget_floor
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


def complete[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a fake I/O coroutine without the broken Windows event loop."""
    try:
        coroutine.send(None)
    except StopIteration as finished:
        # `StopIteration.value` объявлен как `Any`: приводим к тому, что обещает сигнатура.
        return cast(T, finished.value)
    raise AssertionError("The isolated fake unexpectedly required real asynchronous I/O")


def test_full_partial_remove_repeat_restore_one_root(monkeypatch: pytest.MonkeyPatch) -> None:
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
    reread = complete(store.load(CLIENT)).passport
    assert reread is not None
    assert reread.passport.budget.min is None

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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Нижнюю границу убери", True),
        ("Убери нижнюю границу", True),
        ("Нижнюю границу не убери", False),
        ("Не убирай нижнюю границу", False),
        ("Нижнюю границу не нужно убирать", False),
    ],
)
def test_removal_respects_word_order_and_negation(text: str, expected: bool) -> None:
    assert removes_budget_floor(text) is expected


def test_keep_floor_is_noop_and_reversed_removal_edits_same_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        intake_module,
        "get_settings",
        lambda: Settings(broker_project_key="", default_city="nha_trang"),
    )
    store = MemoryStore()
    original = parse_query(BASE, default_city="")
    complete(store.start(Dialogue(user_id=1), original))
    talker = Conversation(store, intake=QueryIntake, finder=_nothing, recorder=SilentJournal())
    first = complete(store.load(CLIENT))
    assert first.passport is not None
    root = first.passport.root

    for message in (
        "Нижнюю границу не убери",
        "Не убирай нижнюю границу",
        "Нижнюю границу оставь",
    ):
        complete(talker.on_text(CLIENT, message, _reply))
        current = complete(store.load(CLIENT))
        assert current.passport is not None
        assert current.passport.root == root
        assert current.passport.version == 1
        assert current.passport.passport.budget.min == 7_000_000

    complete(talker.on_text(CLIENT, "Убери нижнюю границу", _reply))
    removed = complete(store.load(CLIENT))
    assert removed.passport is not None
    assert removed.passport.root == root
    assert removed.passport.version == 2
    assert removed.passport.passport.budget.min is None
    assert {row.root for row in store.rows} == {root}
    assert store.monitoring == {}


@pytest.mark.parametrize(
    ("message", "floor", "ceiling", "zone"),
    [
        ("Убери нижнюю границу, верхнюю поставь 12 млн", None, 12_000_000, "north"),
        ("Нижнюю границу не убирай, подними минимум до 9 млн", 9_000_000, 15_000_000, "north"),
        ("Убери минимум и район поменяй на запад Нячанга", None, 15_000_000, "west"),
    ],
)
def test_compound_edit_applies_all_changes_in_one_root(
    monkeypatch: pytest.MonkeyPatch, message: str, floor: int | None, ceiling: int, zone: str
) -> None:
    from sniffer.domain.districts import PLACE_BY_SLUG

    monkeypatch.setattr(
        intake_module,
        "get_settings",
        lambda: Settings(broker_project_key="", default_city="nha_trang"),
    )
    store = MemoryStore()
    original = parse_query(BASE.replace("от 7", "от 8"), default_city="")
    complete(store.start(Dialogue(user_id=1), original))
    talker = Conversation(store, intake=QueryIntake, finder=_nothing, recorder=SilentJournal())
    first = complete(store.load(CLIENT))
    assert first.passport is not None
    root = first.passport.root
    store.monitoring[root] = "active"

    complete(talker.on_text(CLIENT, message, _reply))
    changed = complete(store.load(CLIENT))
    assert changed.passport is not None
    passport = changed.passport.passport
    assert changed.passport.root == root
    assert changed.passport.version == 2
    assert (passport.budget.min, passport.budget.max) == (floor, ceiling)
    assert passport.category is Category.HOUSE
    assert passport.city == original.city
    assert passport.attributes["rooms"] == original.attributes["rooms"]
    assert passport.attributes["rooms_min"] == original.attributes["rooms_min"]
    assert {PLACE_BY_SLUG[district].zone for district in passport.districts} == {zone}
    assert {row.root for row in store.rows} == {root}
    assert store.monitoring == {root: "active"}


def test_price_floor_removal_preserves_explicit_room_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        intake_module,
        "get_settings",
        lambda: Settings(broker_project_key="", default_city="nha_trang"),
    )
    store = MemoryStore()
    original = parse_query(BASE, default_city="")
    complete(store.start(Dialogue(user_id=1), original))
    talker = Conversation(store, intake=QueryIntake, finder=_nothing, recorder=SilentJournal())
    before = complete(store.load(CLIENT))
    assert before.passport is not None

    complete(
        talker.on_text(CLIENT, "Убери нижнюю границу цены, минимум две комнаты оставь", _reply)
    )
    after = complete(store.load(CLIENT))
    assert after.passport is not None
    assert after.passport.root == before.passport.root
    assert after.passport.passport.budget.min is None
    assert after.passport.passport.budget.max == 15_000_000
    assert after.passport.passport.attributes["rooms"] == 2
    assert after.passport.passport.attributes["rooms_min"] is True

    complete(talker.on_text(CLIENT, "Убери нижнюю границу цены", _reply))
    repeated = complete(store.load(CLIENT))
    assert repeated.passport is not None
    assert repeated.passport.root == after.passport.root
    assert repeated.passport.version == after.passport.version
    assert repeated.passport.passport.budget.min is None


def test_room_minimum_removal_does_not_remove_price_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        intake_module,
        "get_settings",
        lambda: Settings(broker_project_key="", default_city="nha_trang"),
    )
    store = MemoryStore()
    original = parse_query(BASE, default_city="")
    complete(store.start(Dialogue(user_id=1), original))
    talker = Conversation(store, intake=QueryIntake, finder=_nothing, recorder=SilentJournal())
    before = complete(store.load(CLIENT))
    assert before.passport is not None

    complete(talker.on_text(CLIENT, "Убери минимум две комнаты", _reply))
    after = complete(store.load(CLIENT))
    assert after.passport is not None
    assert after.passport.root == before.passport.root
    assert after.passport.passport.budget.min == 7_000_000
    assert after.passport.passport.budget.max == 15_000_000
    assert "rooms" not in after.passport.passport.attributes


def test_mock_model_sees_context_and_can_request_explicit_floor_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        intake_module,
        "get_settings",
        lambda: Settings(broker_project_key="", default_city="nha_trang"),
    )

    class Broker:
        def __init__(self) -> None:
            self.prompts: list[str] = []
            self.response = {
                "budget_min_action": "remove",
                "budget_min_evidence": "без минимальной цены",
            }

        async def structured(self, prompt: str, **_kwargs: object) -> dict[str, str]:
            self.prompts.append(prompt)
            return self.response

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
    broker.response["budget_min_evidence"] = "нижнюю границу не убери"
    _, negated_remove = complete(intake.parse_edit("Нижнюю границу не убери", current))
    assert not negated_remove


ROOM_SEARCH = "Сниму комнату в Нячанге, минимум 2 комнаты, до 7 млн VND в месяц"


def test_room_search_drops_its_room_minimum_without_breaking_the_dialogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Снятие минимума комнат у поиска КОМНАТЫ: ответ, а не исключение, и тот же корень.

    Реестр полей не знает `rooms` у `Category.ROOM`, а парсер его туда кладёт по словам:
    `apply_changes` отвечал на это `EditError` прямо в обработчике сообщения. Перехватить
    его некому, поэтому человек не получал вообще ничего — ни правки, ни отказа. Здесь
    проверяется и то, что условие действительно снято: ответить «уже не задан» было
    нельзя, карточка показывала «не менее 2 комнат».
    """
    monkeypatch.setattr(
        intake_module,
        "get_settings",
        lambda: Settings(broker_project_key="", default_city="nha_trang"),
    )
    store = MemoryStore()
    complete(store.start(Dialogue(user_id=1), parse_query(ROOM_SEARCH, default_city="")))
    talker = Conversation(store, intake=QueryIntake, finder=_nothing, recorder=SilentJournal())
    before = complete(store.load(CLIENT))
    assert before.passport is not None
    assert before.passport.passport.category is Category.ROOM
    assert before.passport.passport.attributes["rooms"] == 2

    complete(talker.on_text(CLIENT, "убери минимум 2 комнаты", _reply))

    after = complete(store.load(CLIENT))
    assert after.passport is not None
    # Тот же поиск: ни нового корня, ни потерянного контекста.
    assert after.passport.root == before.passport.root
    assert {row.root for row in store.rows} == {before.passport.root}
    assert after.passport.passport.category is Category.ROOM
    assert after.passport.passport.city == before.passport.passport.city
    assert after.passport.passport.budget.max == 7_000_000
    # Минимум комнат снят обоими ключами, а не только признаком.
    assert "rooms" not in after.passport.passport.attributes
    assert "rooms_min" not in after.passport.passport.attributes
    # В событие идут ВСЕ снятые ключи: журнал версий описывает ревизию, а не её половину.
    assert store.events[-1].payload["removed"] == ["rooms", "rooms_min"]


def test_room_search_says_so_when_the_minimum_is_already_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Повтор той же просьбы ничего не ломает и новой версии не создаёт."""
    monkeypatch.setattr(
        intake_module,
        "get_settings",
        lambda: Settings(broker_project_key="", default_city="nha_trang"),
    )
    said: list[str] = []

    async def _collect(reply: object) -> None:
        said.append(getattr(reply, "text", ""))

    store = MemoryStore()
    complete(store.start(Dialogue(user_id=1), parse_query(ROOM_SEARCH, default_city="")))
    talker = Conversation(store, intake=QueryIntake, finder=_nothing, recorder=SilentJournal())
    complete(talker.on_text(CLIENT, "убери минимум 2 комнаты", _reply))
    once = complete(store.load(CLIENT))
    assert once.passport is not None

    complete(talker.on_text(CLIENT, "убери минимум 2 комнаты", _collect))

    twice = complete(store.load(CLIENT))
    assert twice.passport is not None
    assert twice.passport.root == once.passport.root
    assert twice.passport.version == once.passport.version
    assert said == ["Минимум комнат уже не задан."]


def test_a_refused_edit_is_words_not_a_traceback() -> None:
    """Любой отказ домена превращается в `None`, а не в исключение из обработчика.

    Ноль для «бюджета от» сегодня не доходит до этой правки (разбор суммы его не отдаёт),
    и именно поэтому проверка идёт прямо по `_edited`: ворота должны держать весь класс
    отказов `EditError`, а не тот единственный случай, который удалось придумать.
    """
    passport = parse_query(BASE, default_city="")
    assert _edited(passport, Change("budget_min", 9_000_000)) is not None
    assert _edited(passport, Change("budget_min", 0)) is None
    assert _edited(passport, Change("city")) is None
    assert _edited(parse_query(ROOM_SEARCH, default_city=""), Change("rooms")) is None


def test_a_bedroom_minimum_is_removed_and_not_declared_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Минимум спален снимается той же просьбой, а не объявляется несуществующим.

    Про спальни парсер пишет `bedrooms` и ВЫБРАСЫВАЕТ `rooms`, а охрана спрашивала
    только `rooms`: бот отвечал «Минимум комнат уже не задан», пока условие оставалось в
    паспорте и продолжало отсеивать находки. Та самая ложь, которой этот обработчик
    обещает избегать, — только в другом ключе.
    """
    monkeypatch.setattr(
        intake_module,
        "get_settings",
        lambda: Settings(broker_project_key="", default_city="nha_trang"),
    )
    said: list[str] = []

    async def _collect(reply: object) -> None:
        said.append(getattr(reply, "text", ""))

    store = MemoryStore()
    flat = "Сниму квартиру в Нячанге, минимум 2 спальни, до 15 млн VND в месяц"
    complete(store.start(Dialogue(user_id=1), parse_query(flat, default_city="")))
    talker = Conversation(store, intake=QueryIntake, finder=_nothing, recorder=SilentJournal())
    before = complete(store.load(CLIENT))
    assert before.passport is not None
    assert before.passport.passport.attributes["bedrooms"] == 2

    complete(talker.on_text(CLIENT, "убери минимум 2 спальни", _collect))

    after = complete(store.load(CLIENT))
    assert after.passport is not None
    assert after.passport.root == before.passport.root
    assert "bedrooms" not in after.passport.passport.attributes
    assert after.passport.passport.budget.max == 15_000_000
    assert store.events[-1].payload["removed"] == ["bedrooms"]
    assert "уже не задан" not in " ".join(said)


def test_the_room_minimum_stays_removable_by_the_registry() -> None:
    """Снятие `rooms` держится на двух свойствах реестра — закрепляем их тестом.

    `_without_room_minimum` снимает атрибут сам, когда реестр отказал, и это верно, пока
    отказ означает «поле чужой категории». Пометят `rooms` как `required` или повесят на
    него `companions` — и обход снимет то, что домен снимать запретил, а тесты останутся
    зелёными: они проверяют, что отказ есть, но не проверяют его причину.
    """
    spec = spec_by_key("rooms")
    assert spec is not None
    assert spec.required is False
    assert spec.companions == ()
