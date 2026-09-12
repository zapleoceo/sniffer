"""Ответ из собственного каталога: паспорт → одна архивная задача → тот же отбор.

Базы и сети нет: `run_plan` и курс подменены. Проверяется устройство пути, а
не SQL (он — в `test_db_repositories`): одна задача архивному источнику с
нейтральными параметрами паспорта, долларовый бюджет в донгах, общий
`rank_items`, маршрут режима `listings` в `CatalogFinder` и то, как
`search_listings` переводит параметры в `MatchFilter`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from sniffer.bot import catalog_search
from sniffer.bot.catalog_finder import CatalogFinder
from sniffer.bot.catalog_search import catalog_plan, find_catalog
from sniffer.bot.conversation import Found
from sniffer.config import Settings
from sniffer.domain.passport import engine_cc_bounds
from sniffer.domain.records import MatchFilter
from sniffer.search.intake_rules import parse_query
from sniffer.search.plan import SearchPlan
from sniffer.sources import chat_directory
from sniffer.sources.base import RawItem
from tests.test_catalog_dialogue import scope

CITY = "nha_trang"


def test_catalog_plan_is_one_archive_task_carrying_the_passport() -> None:
    """Архивный источник ищет полями: текст задачи пуст, всё едет параметрами."""
    passport = parse_query("honda lead автомат до 500 долларов", default_city=CITY)
    plan = catalog_plan(passport)

    (task,) = plan.tasks
    assert (task.source, task.query) == ("archive", "")
    assert task.params["city"] == CITY
    assert task.params["category"] == "motorbike"
    assert task.params["intent"] == "buy"
    assert task.params["attributes"]["model"] == "lead"
    assert task.params["attributes"]["transmission"] == "automatic"
    assert task.params["budget"]["max"] == 500.0
    assert task.params["budget"]["currency"] == "USD"


def _item(source: str, external_id: str, price_vnd: int, *, age_days: int) -> RawItem:
    # Тексты РАЗНЫЕ намеренно: одинаковые схлопнул бы дедуп кросспостов
    # (`relevance._dedup`) — один лот, переопубликованный дважды, это одна
    # карточка, и тест о бюджете мерил бы тогда дедуп.
    return RawItem(
        source=source,
        external_id=external_id,
        url=f"https://example.test/{external_id}",
        title=f"Honda Lead 2019, автомат, лот {external_id}",
        text=f"Продам Honda Lead 125, автомат, документы, объявление {external_id}",
        price_vnd=price_vnd,
        posted_at=datetime.now(UTC) - timedelta(days=age_days),
    )


async def test_find_catalog_puts_the_usd_budget_into_vnd_and_ranks_with_the_shared_ranker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[SearchPlan] = []
    within = _item("telegram_archive", "1", 11_000_000, age_days=1)
    pricey = _item("chotot", "2", 40_000_000, age_days=0)

    async def run_plan(plan: SearchPlan, *, budget_s: float = 90.0) -> list[RawItem]:
        seen.append(plan)
        return [pricey, within]

    async def rate() -> float:
        return 25_000.0

    monkeypatch.setattr(catalog_search, "run_plan", run_plan)
    monkeypatch.setattr(catalog_search, "usd_vnd_rate", rate)

    found = await find_catalog(parse_query("honda lead до 500 долларов", default_city=CITY))

    (task,) = seen[0].tasks
    assert task.params["budget"]["currency"] == "VND"
    assert task.params["budget"]["max"] == 12_500_000.0
    # 40 млн при потолке 12.5 млн — противоречие запросу, а не «чуть дороже»:
    # отбор тот же, что у живого поиска.
    assert [item.external_id for item in found.items] == ["1"]
    assert found.sources == ("telegram_archive",)
    assert set(found.stages) == {"plan_ms", "search_ms"}
    assert not found.fallback


async def test_without_a_rate_a_usd_budget_narrows_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Врать курсом нельзя: нет курса — нет потолка ни в SQL, ни в отборе."""
    items = [_item("chotot", "2", 40_000_000, age_days=0), _item("chotot", "1", 1, age_days=0)]

    async def run_plan(plan: SearchPlan, *, budget_s: float = 90.0) -> list[RawItem]:
        assert plan.tasks[0].params["budget"]["currency"] == "USD"
        return items

    async def no_rate() -> None:
        return None

    monkeypatch.setattr(catalog_search, "run_plan", run_plan)
    monkeypatch.setattr(catalog_search, "usd_vnd_rate", no_rate)

    found = await find_catalog(parse_query("honda lead до 500 долларов", default_city=CITY))
    assert {item.external_id for item in found.items} == {"1", "2"}


async def test_listings_mode_routes_to_the_catalog_and_nowhere_else() -> None:
    legacy, catalog = AsyncMock(), AsyncMock()
    listings = AsyncMock(return_value=Found([], sources=("telegram_archive",)))
    finder = CatalogFinder(
        legacy=legacy,
        catalog=catalog,
        listings=listings,
        settings=lambda: Settings(catalog_mode="listings"),
    )
    dialogue = scope()

    result = await finder(dialogue)

    assert dialogue.passport is not None
    listings.assert_awaited_once_with(dialogue.passport.passport)
    legacy.assert_not_awaited()
    catalog.assert_not_awaited()
    assert result.sources == ("telegram_archive",)


async def test_search_listings_turns_neutral_params_into_a_catalog_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Те же имена, что читает разбор запроса: марка, модель, коробка, объём."""
    captured: list[MatchFilter] = []

    class Repository:
        def __init__(self, session: object) -> None:
            self.session = session

        async def search_catalog(self, spec: MatchFilter, *, limit: int) -> list[object]:
            captured.append(spec)
            return []

    @asynccontextmanager
    async def sessions() -> AsyncIterator[object]:
        yield object()

    monkeypatch.setattr("sniffer.db.ListingRepository", Repository)
    monkeypatch.setattr("sniffer.db.session_scope", sessions)

    params: dict[str, object] = {
        "city": CITY,
        "category": "motorbike",
        "intent": "buy",
        "attributes": {
            "brand": "honda",
            "model": "lead",
            "transmission": "automatic",
            "engine_cc": 250,
            "engine_cc_dir": "min",
            # Документы — мягкий сигнал балла, а не отсев (passport.md): в SQL
            # им делать нечего.
            "papers": "blue_card",
        },
        "budget": {"min": 0, "max": 12_500_000.0, "currency": "VND", "period": "once"},
    }
    assert await chat_directory.search_listings(params, limit=100) == []

    (spec,) = captured
    assert spec.city == CITY
    assert spec.category == "motorbike"
    assert spec.deal_type == "sell"
    assert spec.attributes == {"brand": "honda", "transmission": "automatic"}
    assert spec.model == "lead"
    assert (spec.engine_cc_min, spec.engine_cc_max) == (250, None)
    assert spec.max_price_vnd == Decimal("12500000.0")
    assert spec.since is not None
    assert spec.since > datetime.now(UTC) - timedelta(days=chat_directory.CATALOG_MAX_AGE_DAYS + 1)


@pytest.mark.parametrize(
    ("engine_cc", "direction", "bounds"),
    [
        (200, None, (150, 250)),
        (250, "min", (250, None)),
        (125, "max", (None, 125)),
        ("200", None, (None, None)),
        (True, None, (None, None)),
        (None, "min", (None, None)),
    ],
)
def test_engine_bounds_follow_the_direction(
    engine_cc: object, direction: object, bounds: tuple[int | None, int | None]
) -> None:
    assert engine_cc_bounds(engine_cc, direction) == bounds
