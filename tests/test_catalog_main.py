"""MCP request isolation and deterministic cards with broker failure simulations."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest

from sniffer.agent_app import main, main_gateway
from sniffer.agent_app.contracts import MainIdentity
from sniffer.agent_app.main_gateway import MainGateway, collection_scope
from sniffer.broker.client import BrokerResult
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.domain.records import StoredPassport


@pytest.fixture
def repos(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    passport = Passport(city="nha_trang", category=Category.MOTORBIKE, intent=Intent.BUY)
    stored = StoredPassport(id=10, user_id=7, version=2, passport=passport, root_id=9)
    ownership = AsyncMock()
    ownership.owned.return_value = stored
    catalog = AsyncMock()
    catalog.search.return_value = []
    catalog.coverage.return_value = {"sources": {"chotot": "not_collected", "archive": "fresh"}}
    tasks = AsyncMock()
    tasks.status_for.return_value = []
    tasks.enqueue.return_value = 22
    session = AsyncMock()

    @asynccontextmanager
    async def sessions() -> Any:
        yield session

    monkeypatch.setattr(main_gateway, "AgentRequestRepository", lambda s: ownership)
    monkeypatch.setattr(main, "AgentRequestRepository", lambda s: ownership)
    monkeypatch.setattr(main_gateway, "CatalogObservationRepository", lambda s: catalog)
    monkeypatch.setattr(main_gateway, "CollectionTaskRepository", lambda s: tasks)
    monkeypatch.setattr(
        main_gateway,
        "get_settings",
        lambda: type("Config", (), {"agent_collector_enabled": True})(),
    )
    return {
        "owned": ownership,
        "catalog": catalog,
        "tasks": tasks,
        "sessions": sessions,
        "session": session,
        "stored": stored,
    }


async def test_server_supplies_only_owned_current_request_filters(repos: dict[str, Any]) -> None:
    gateway = MainGateway(MainIdentity(7, 9, 2), repos["sessions"])
    assert await gateway.call("catalog_search", {}) == {"items": [], "count": 0}
    repos["owned"].owned.assert_awaited_once_with(7, 9, 2)
    assert repos["catalog"].search.call_args.kwargs["city"] == "nha_trang"
    with pytest.raises(PermissionError):
        await gateway.call("catalog_search", {"user_id": 8})
    with pytest.raises(PermissionError):
        await gateway.call("execute_sql", {})


async def test_exact_model_reaches_database_before_catalog_limit(repos: dict[str, Any]) -> None:
    stored = repos["stored"]
    repos["owned"].owned.return_value = StoredPassport(
        id=stored.id,
        user_id=stored.user_id,
        version=stored.version,
        root_id=stored.root_id,
        passport=stored.passport.model_copy(
            update={"attributes": {"brand": "honda", "model": "zoomer"}}
        ),
    )

    gateway = MainGateway(MainIdentity(7, 9, 2), repos["sessions"])
    await gateway.call("catalog_search", {})

    criteria = repos["catalog"].search.call_args.kwargs
    assert criteria["brand"] == "honda"
    assert criteria["model"] == "zoomer"


def test_exact_request_criteria_get_distinct_collection_coverage() -> None:
    broad = Passport(city="nha_trang", category=Category.MOTORBIKE, intent=Intent.BUY)
    exact = broad.model_copy(
        deep=True,
        update={
            "attributes": {
                "brand": "honda",
                "model": "lead",
                "transmission": "automatic",
                "engine_cc": 125,
                "engine_cc_dir": "max",
            },
            "budget": Budget(max=500, currency=Currency.USD),
        },
    )
    first, second = collection_scope(broad), collection_scope(exact)
    assert first.criteria.key != second.criteria.key
    assert second.criteria.brand == "honda" and second.criteria.model == "lead"
    assert second.criteria.engine_cc == 125 and second.criteria.budget_max == 500


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("transmission", "semi"),
        ("brand", "honda"),
        ("model", "lead"),
        ("engine_cc", 125),
    ],
)
def test_each_motorbike_axis_changes_collection_identity(field: str, value: object) -> None:
    broad = Passport(city="nha_trang", category=Category.MOTORBIKE, intent=Intent.BUY)
    narrowed = broad.model_copy(update={"attributes": {field: value}})
    scope = collection_scope(narrowed)
    assert scope.criteria.key != collection_scope(broad).criteria.key
    assert getattr(scope.criteria, field) == value


@pytest.mark.parametrize(
    ("category", "intent", "sources"),
    [
        (Category.MOTORBIKE, Intent.BUY, ("chotot", "archive")),
        (Category.MOTORBIKE, Intent.SELL, ("archive",)),
        (Category.APARTMENT, Intent.RENT, ("archive",)),
        (Category.ROOM, Intent.RENT, ("archive",)),
    ],
)
def test_only_source_verified_for_category_is_assigned(
    category: Category, intent: Intent, sources: tuple[str, ...]
) -> None:
    passport = Passport(city="da_nang", category=category, intent=intent)
    assert collection_scope(passport).sources == sources


async def test_foreign_or_stale_request_does_not_read_catalogue(repos: dict[str, Any]) -> None:
    repos["owned"].owned.side_effect = PermissionError("stale")
    gateway = MainGateway(MainIdentity(8, 9, 1), repos["sessions"])
    with pytest.raises(PermissionError):
        await gateway.call("catalog_search", {})
    repos["catalog"].search.assert_not_awaited()


async def test_missing_coverage_queues_sanitized_shared_scope(repos: dict[str, Any]) -> None:
    gateway = MainGateway(MainIdentity(7, 9, 2), repos["sessions"])
    status = await gateway.queue_if_needed()
    assert status and "22" in status
    args = repos["tasks"].enqueue.call_args
    assert args.kwargs["user_id"] == 7 and args.kwargs["request_version"] == 2
    assert "raw_query" not in args.args[0] and "user_id" not in args.args[0]
    repos["session"].commit.assert_awaited_once()


async def test_unsupported_budget_currency_fails_closed_without_collection(
    repos: dict[str, Any],
) -> None:
    stored = repos["stored"]
    repos["owned"].owned.return_value = StoredPassport(
        id=stored.id,
        user_id=stored.user_id,
        version=stored.version,
        root_id=stored.root_id,
        passport=stored.passport.model_copy(
            update={"budget": Budget(max=400, currency=Currency.EUR)}
        ),
    )
    gateway = MainGateway(MainIdentity(7, 9, 2), repos["sessions"])
    assert await gateway.call("catalog_search", {}) == {"count": 0, "items": []}
    assert "VND" in (await gateway.queue_if_needed() or "")
    repos["catalog"].search.assert_not_awaited()
    repos["tasks"].enqueue.assert_not_awaited()


async def test_pending_job_is_not_duplicated_at_next_hour(repos: dict[str, Any]) -> None:
    repos["tasks"].status_for.return_value = [{"id": 11, "status": "running"}]
    gateway = MainGateway(MainIdentity(7, 9, 2), repos["sessions"])
    assert "выполняется" in (await gateway.queue_if_needed() or "")
    repos["tasks"].enqueue.assert_not_awaited()


async def test_fresh_empty_result_and_shadow_never_create_work(repos: dict[str, Any]) -> None:
    repos["catalog"].coverage.return_value = {"sources": {"chotot": "fresh", "archive": "fresh"}}
    gateway = MainGateway(MainIdentity(7, 9, 2), repos["sessions"])
    assert await gateway.queue_if_needed() is None
    shadow = MainGateway(MainIdentity(7, 9, 2, False), repos["sessions"])
    assert await shadow.queue_if_needed() is None
    repos["tasks"].enqueue.assert_not_awaited()


@pytest.mark.parametrize("failure", [False, True])
async def test_broker_prose_is_not_a_card_and_failure_still_reads_catalogue(
    repos: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    failure: bool,
) -> None:
    gateway = MainGateway(MainIdentity(7, 9, 2, False), repos["sessions"])
    monkeypatch.setattr(main, "MainGateway", lambda identity: gateway)
    broker = AsyncMock()
    broker.chat.return_value = BrokerResult(
        text="Invented scooter $1 https://fake.test", finish_reason="stop"
    )
    if failure:
        broker.chat.side_effect = RuntimeError("private raw provider reply")
    monkeypatch.setattr(main, "BrokerClient", lambda **kw: broker)
    answer = await main.search_request(7, 9, 2, allow_collection=False)
    assert answer.items == [] and answer.status is None
    repos["catalog"].search.assert_awaited_once()
    broker.aclose.assert_awaited_once()


async def test_exact_model_skips_the_redundant_catalog_agent(
    repos: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = repos["stored"]
    repos["owned"].owned.return_value = StoredPassport(
        id=stored.id,
        user_id=stored.user_id,
        version=stored.version,
        root_id=stored.root_id,
        passport=stored.passport.model_copy(
            update={"attributes": {"brand": "honda", "model": "zoomer"}}
        ),
    )
    gateway = MainGateway(MainIdentity(7, 9, 2, False), repos["sessions"])
    monkeypatch.setattr(main, "MainGateway", lambda identity: gateway)

    def unexpected_broker(**kwargs: object) -> object:
        raise AssertionError("an exact request needs no second model call")

    monkeypatch.setattr(main, "BrokerClient", unexpected_broker)

    answer = await main.search_request(7, 9, 2, allow_collection=False)

    assert answer.items == []
    repos["catalog"].search.assert_awaited_once()


async def test_missing_usd_rate_is_not_silently_ignored(
    repos: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    repos["stored"].passport.budget = Budget(max=500, currency=Currency.USD)
    gateway = MainGateway(MainIdentity(7, 9, 2, False), repos["sessions"])
    monkeypatch.setattr(main, "MainGateway", lambda identity: gateway)
    broker = AsyncMock()
    broker.chat.return_value = BrokerResult(text="done", finish_reason="stop")
    monkeypatch.setattr(main, "BrokerClient", lambda **kw: broker)
    monkeypatch.setattr(main, "usd_vnd_rate", AsyncMock(return_value=None))
    answer = await main.search_request(7, 9, 2, allow_collection=False)
    assert not answer.items and "курс" in (answer.status or "")
