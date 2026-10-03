"""Контракт хранилища диалога: одни и те же сценарии на подделке и на настоящей базе.

Подделка (`simulation.stubs.MemoryStore`) нужна, чтобы тесты диалога и симулятор шли
без Postgres. Её цена — риск разойтись с `PassportStore`, и расходилась она уже:
правка паспорта снимала взведённое `/new` в базе (`save_revision` зовёт `select`) и
не снимала в подделке, а тесты и симулятор показывали одно поведение, прод — другое.

Поэтому поведение, на которое опирается бот, описано сценариями, и каждый идёт ДВАЖДЫ:
на подделке всегда и на `PassportStore` — когда есть `TEST_DATABASE_URL` (в CI он
есть всегда). Сценарий, который проходит на одной реализации и краснеет на другой, и
есть найденное расхождение.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from sniffer.bot.store import Client, Dialogue, DialogueStore, PassportStore
from sniffer.db import collection_models as _collection_models  # noqa: F401
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.domain.threads import MAX_LIVE_THREADS
from sniffer.simulation.stubs import MemoryStore

CLIENT = Client(tg_user_id=42, username="dima")
STRANGER = Client(tg_user_id=77, username="stranger")


@dataclass(frozen=True)
class Rig:
    """Хранилище и способ «перезапустить бота»: для подделки — то же, для базы — новое."""

    make: Callable[[], DialogueStore]
    client: Client = CLIENT


Scenario = Callable[[Rig], Awaitable[None]]


def passport(number: int = 1) -> Passport:
    return Passport(
        intent=Intent.BUY,
        category=Category.MOTORBIKE,
        city="nha_trang",
        budget=Budget(max=100 + number, currency=Currency.USD),
        raw_query=f"ищу скутер {number}",
    )


async def open_threads(rig: Rig, count: int) -> tuple[Dialogue, list[int]]:
    """Столько поисков подряд. Возвращает разговор после последнего и корни по порядку."""
    store = rig.make()
    dialogue = await store.load(rig.client)
    roots: list[int] = []
    for number in range(1, count + 1):
        dialogue = await store.start(dialogue, passport(number))
        assert dialogue.passport is not None
        roots.append(dialogue.passport.root)
    return dialogue, roots


async def armed(rig: Rig, count: int = 2) -> tuple[Dialogue, list[int]]:
    """Несколько поисков и взведённое `/new`: разговор уже прочитан заново."""
    dialogue, roots = await open_threads(rig, count)
    store = rig.make()
    await store.await_new(dialogue)
    reloaded = await store.load(rig.client)
    assert reloaded.starting_new is True, "флаг не взвёлся — дальше проверять нечего"
    return reloaded, roots


# ── что снимает и что хранит флаг `/new` ────────────────────────────────────


async def the_flag_survives_a_restart(rig: Rig) -> None:
    dialogue, _roots = await open_threads(rig, 1)
    await rig.make().await_new(dialogue)

    restarted = await rig.make().load(rig.client)

    assert restarted.starting_new is True


async def a_plain_start_disarms(rig: Rig) -> None:
    dialogue, _roots = await armed(rig)

    await rig.make().start(dialogue, passport(9))

    assert (await rig.make().load(rig.client)).starting_new is False


async def a_choice_disarms(rig: Rig) -> None:
    dialogue, roots = await armed(rig)

    chosen = await rig.make().select(dialogue, roots[0])

    assert chosen.starting_new is False
    assert (await rig.make().load(rig.client)).starting_new is False


async def an_edit_disarms(rig: Rig) -> None:
    """Правка — тоже выбор: в базе её делает `save_revision` через `select`."""
    dialogue, _roots = await armed(rig)

    await rig.make().revise(dialogue, passport(8), kind="manual_edit", payload={"field": "x"})

    assert (await rig.make().load(rig.client)).starting_new is False


async def a_note_keeps_the_flag(rig: Rig) -> None:
    """Событие без правки паспорта ветку не выбирает — `/new` остаётся взведённым."""
    dialogue, _roots = await armed(rig)

    await rig.make().note(dialogue, kind="question_asked", payload={"field": "category"})

    assert (await rig.make().load(rig.client)).starting_new is True


async def a_foreign_root_is_not_selectable_and_does_not_disarm(rig: Rig) -> None:
    dialogue, _roots = await armed(rig)
    other = rig.make()
    stranger = await other.load(STRANGER)
    stranger = await other.start(stranger, passport(5))
    assert stranger.passport is not None

    unchanged = await rig.make().select(dialogue, stranger.passport.root)

    assert unchanged.passport == dialogue.passport
    assert (await rig.make().load(rig.client)).starting_new is True


# ── `/new`: одно взведение — один поиск ─────────────────────────────────────


async def start_requested_spends_the_flag_once(rig: Rig) -> None:
    stale, _roots = await armed(rig)
    store = rig.make()

    first = await store.start_requested(stale, passport(6))
    second = await store.start_requested(stale, passport(7))

    assert first is not None
    assert second is None, "второй со старым снимком проиграл: поиск уже открыт"
    after = await rig.make().load(rig.client)
    assert after.starting_new is False
    assert len(await rig.make().live_threads(after)) == 3, "два прежних и ОДИН новый"


async def start_requested_without_the_flag_is_refused(rig: Rig) -> None:
    dialogue, _roots = await open_threads(rig, 2)

    refused = await rig.make().start_requested(dialogue, passport(6))

    assert refused is None
    assert len(await rig.make().live_threads(dialogue)) == 2


# ── предел и порядок списка ─────────────────────────────────────────────────


async def the_list_is_capped_at_the_limit(rig: Rig) -> None:
    dialogue, roots = await open_threads(rig, MAX_LIVE_THREADS + 1)

    live = await rig.make().live_threads(dialogue)

    assert len(live) == MAX_LIVE_THREADS
    assert roots[0] not in {item.root for item in live}, "вытеснен самый давно не использованный"


async def a_choice_returns_a_pushed_out_search_to_the_list(rig: Rig) -> None:
    """Обещание «выбор возвращает поиск в список» — это порядок по использованию.

    Раньше список упорядочивал `created_at` версии, `select` его не трогал, и выбранный
    вытесненный поиск оставался активным, но невидимым: ни строки, ни «✓».
    """
    dialogue, roots = await open_threads(rig, MAX_LIVE_THREADS + 1)
    store = rig.make()
    assert roots[0] not in {item.root for item in await store.live_threads(dialogue)}

    back = await store.select(dialogue, roots[0])

    assert back.passport is not None and back.passport.root == roots[0]
    live = await store.live_threads(back)
    assert live[0].root == roots[0], "выбранный — наверху"
    assert len(live) == MAX_LIVE_THREADS
    assert roots[1] not in {item.root for item in live}, "вытеснен следующий по давности"
    assert [item.is_active for item in live] == [True] + [False] * (MAX_LIVE_THREADS - 1)


async def an_edit_lifts_a_search_in_the_list(rig: Rig) -> None:
    dialogue, roots = await open_threads(rig, 3)
    store = rig.make()
    back = await store.select(dialogue, roots[0])
    await store.revise(back, passport(9), kind="manual_edit", payload={"field": "x"})

    live = await store.live_threads(await store.load(rig.client))

    assert live[0].root == roots[0]


SCENARIOS: list[Scenario] = [
    the_flag_survives_a_restart,
    a_plain_start_disarms,
    a_choice_disarms,
    an_edit_disarms,
    a_note_keeps_the_flag,
    a_foreign_root_is_not_selectable_and_does_not_disarm,
    start_requested_spends_the_flag_once,
    start_requested_without_the_flag_is_refused,
    the_list_is_capped_at_the_limit,
    a_choice_returns_a_pushed_out_search_to_the_list,
    an_edit_lifts_a_search_in_the_list,
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.__name__)
async def test_the_memory_store_keeps_the_contract(scenario: Scenario) -> None:
    store = MemoryStore()

    await scenario(Rig(make=lambda: store))


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.__name__)
async def test_the_postgres_store_keeps_the_contract(
    scenario: Scenario, db_engine: AsyncEngine
) -> None:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)

    await scenario(Rig(make=lambda: PassportStore(lambda: sessions())))


# ── интерфейс: подделка и боевое хранилище не расходятся по сигнатурам ──────


def _shape(owner: type, name: str) -> list[tuple[str, str]]:
    parameters = inspect.signature(getattr(owner, name)).parameters.values()
    return [(parameter.name, parameter.kind.name) for parameter in parameters]


def test_the_fake_and_the_real_store_have_the_same_interface() -> None:
    """Метод есть в протоколе и в базе, а в подделке нет — тесты диалога падали бы позже и глуше."""
    protocol = {
        name
        for name, member in inspect.getmembers(DialogueStore, inspect.isfunction)
        if not name.startswith("_")
    }

    assert protocol >= {"load", "start", "start_requested", "revise", "note", "select"}
    for name in sorted(protocol):
        assert (
            _shape(MemoryStore, name) == _shape(PassportStore, name) == _shape(DialogueStore, name)
        ), name
