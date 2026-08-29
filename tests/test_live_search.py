"""Исполнение плана поиска: бюджет, дедуп, поведение при сломанном источнике.

Сети здесь нет и быть не может: адаптер подменён заглушкой. Проверяется не то,
что источник отвечает, а то, что исполнитель соблюдает бюджет spec-v2 (2.3) и
не даёт одному источнику испортить выдачу остальных.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sniffer.search import live
from sniffer.search.live import MAX_TASKS_PER_SOURCE, run_plan
from sniffer.search.plan import SearchPlan, SearchTask
from sniffer.sources.base import RawItem, Source, UnknownSourceError

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


class StubSource(Source):
    """Адаптер без сети: помнит запросы и отдаёт заранее заготовленные лоты."""

    name = "stub"

    def __init__(
        self,
        items: list[RawItem] | None = None,
        *,
        crash: Exception | None = None,
        degrade_after: int | None = None,
        hang: bool = False,
    ) -> None:
        super().__init__()
        self._items = items or []
        self._crash = crash
        self._degrade_after = degrade_after
        self._hang = hang
        self.queries: list[str] = []
        self.closed = False

    async def search(self, query: str, params: dict[str, Any]) -> list[RawItem]:
        self.queries.append(query)
        if self._hang:
            await asyncio.sleep(30)
        if self._crash is not None:
            raise self._crash
        if self._degrade_after is not None and len(self.queries) >= self._degrade_after:
            self.degraded = True
        return list(self._items)

    async def aclose(self) -> None:
        self.closed = True


def raw(external_id: str, *, source: str = "stub", age_days: int = 1) -> RawItem:
    return RawItem(
        source=source,
        external_id=external_id,
        url=f"https://example.test/{external_id}",
        title=f"лот {external_id}",
        posted_at=NOW - timedelta(days=age_days),
    )


def plan_of(*tasks: tuple[str, str]) -> SearchPlan:
    return SearchPlan(tasks=[SearchTask(source=source, query=query) for source, query in tasks])


def use(monkeypatch: pytest.MonkeyPatch, adapters: dict[str, Source]) -> None:
    def get_source(name: str, **_kwargs: Any) -> Source:
        try:
            return adapters[name]
        except KeyError as exc:
            raise UnknownSourceError(name) from exc

    monkeypatch.setattr(live, "get_source", get_source)


async def test_same_lot_from_two_queries_is_one_card(monkeypatch: pytest.MonkeyPatch) -> None:
    source = StubSource([raw("1"), raw("2")])
    use(monkeypatch, {"stub": source})

    items = await run_plan(plan_of(("stub", "скутер"), ("stub", "инжектор")))

    assert [item.external_id for item in items] == ["1", "2"]
    assert source.queries == ["скутер", "инжектор"]


async def test_fresh_lots_come_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Свежесть решает: вчерашний байк ещё продаётся, майский почти наверняка нет."""
    old = raw("old", age_days=40)
    fresh = raw("fresh", age_days=1)
    undated = RawItem(source="stub", external_id="undated", url="https://example.test/u")
    use(monkeypatch, {"stub": StubSource([old, undated, fresh])})

    items = await run_plan(plan_of(("stub", "скутер")))

    assert [item.external_id for item in items] == ["fresh", "old", "undated"]


async def test_source_budget_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    source = StubSource([raw("1")])
    use(monkeypatch, {"stub": source})
    tasks = [("stub", f"запрос {index}") for index in range(MAX_TASKS_PER_SOURCE + 4)]

    await run_plan(plan_of(*tasks))

    assert len(source.queries) == MAX_TASKS_PER_SOURCE


async def test_degraded_source_is_not_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    source = StubSource([raw("1")], degrade_after=1)
    use(monkeypatch, {"stub": source})

    await run_plan(plan_of(("stub", "раз"), ("stub", "два"), ("stub", "три")))

    assert source.queries == ["раз"]


async def test_broken_source_does_not_take_the_others_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = StubSource(crash=RuntimeError("адаптер уронил контракт"))
    working = StubSource([raw("ok", source="other")])
    use(monkeypatch, {"stub": broken, "other": working})

    items = await run_plan(plan_of(("stub", "скутер"), ("other", "xe ga")))

    assert [item.external_id for item in items] == ["ok"]


async def test_slow_source_gives_up_and_others_are_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Что не уложилось в бюджет — отдаётся частично, а не теряется целиком."""
    slow = StubSource(hang=True)
    quick = StubSource([raw("ok", source="other")])
    use(monkeypatch, {"stub": slow, "other": quick})

    items = await asyncio.wait_for(
        run_plan(plan_of(("stub", "скутер"), ("other", "xe ga")), budget_s=0.05),
        timeout=2,
    )

    assert [item.external_id for item in items] == ["ok"]
    assert slow.closed, "клиент зависшего источника всё равно надо закрыть"


async def test_adapters_are_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Незакрытый httpx-клиент — утечка сокетов на каждый запрос клиента."""
    source = StubSource([raw("1")])
    use(monkeypatch, {"stub": source})

    await run_plan(plan_of(("stub", "скутер")))

    assert source.closed


async def test_source_without_adapter_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Реестр источников живёт в БД и может опережать код."""
    working = StubSource([raw("ok")])
    use(monkeypatch, {"stub": working})

    items = await run_plan(plan_of(("facebook", "что угодно"), ("stub", "скутер")))

    assert [item.external_id for item in items] == ["ok"]


async def test_empty_plan_costs_nothing() -> None:
    assert await run_plan(SearchPlan()) == []
