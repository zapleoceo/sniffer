"""Пауза и «Удалить поиск» освобождают слот, возобновление просит свободный (на подделках)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from sniffer.bot import query_menu, watch_flow
from sniffer.bot.store import Client
from sniffer.domain.slots import Outcome, SlotState

CLIENT = Client(tg_user_id=5)


class Calls:
    def __init__(self, *, enable: Outcome = Outcome.ENABLE, paused: bool = True) -> None:
        self.log: list[str] = []
        self.enable = enable
        self.paused = paused


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    seen = Calls()

    class Session:
        async def commit(self) -> None:
            seen.log.append("commit")

    @asynccontextmanager
    async def scope() -> AsyncIterator[Session]:
        yield Session()

    class Users:
        def __init__(self, _s: Any) -> None: ...

        async def get_or_create(self, *_a: Any, **_k: Any) -> Any:
            return SimpleNamespace(id=1)

    class Delivery:
        def __init__(self, _s: Any) -> None: ...

        async def set_active(self, **kw: Any) -> bool:
            seen.log.append(f"set_active:{kw['active']}")
            return seen.paused

    class Slots:
        def __init__(self, _s: Any) -> None: ...

        async def sync(self, *_a: Any) -> SlotState:
            seen.log.append("sync")
            return SlotState()

        async def enable(self, *_a: Any) -> tuple[Outcome, SlotState]:
            seen.log.append("enable")
            return seen.enable, SlotState()

    class Watch:
        def __init__(self, _s: Any) -> None: ...

        async def archive(self, *_a: Any) -> bool:
            seen.log.append("archive")
            return True

    for module in (query_menu, watch_flow):
        monkeypatch.setattr(module, "session_scope", scope)
        monkeypatch.setattr(module, "SlotRepository", Slots, raising=module is query_menu)
    monkeypatch.setattr(query_menu, "UserRepository", Users)
    monkeypatch.setattr(query_menu, "DeliveryRepository", Delivery)
    monkeypatch.setattr(watch_flow, "UserRepository", Users)
    monkeypatch.setattr(watch_flow, "WatchRepository", Watch)
    return seen


async def test_a_pause_frees_the_slot_by_recounting_in_the_same_transaction(calls: Calls) -> None:
    assert await query_menu.toggle(CLIENT, 7, active=False)
    assert calls.log == ["set_active:False", "sync", "commit"]


async def test_a_pause_that_changed_nothing_does_not_recount(calls: Calls) -> None:
    calls.paused = False
    assert not await query_menu.toggle(CLIENT, 7, active=False)
    assert "sync" not in calls.log


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (Outcome.ENABLE, True),
        (Outcome.ALREADY_ON, True),
        (Outcome.NO_FREE_SLOT, False),
        (Outcome.NEEDS_SUBSCRIPTION, False),
    ],
)
async def test_resuming_needs_a_free_slot_like_follow(
    calls: Calls, outcome: Outcome, expected: bool
) -> None:
    calls.enable = outcome
    assert await query_menu.toggle(CLIENT, 7, active=True) is expected
    assert "set_active:True" not in calls.log, "возобновление идёт через слот, а не флагом"


async def test_deleting_a_search_frees_its_slot(calls: Calls) -> None:
    assert await watch_flow.archive(CLIENT, 7)
    assert calls.log[-3:] == ["archive", "sync", "commit"]
