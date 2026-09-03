"""MCP request isolation and deterministic cards with broker failure simulations."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest

from sniffer.agent_app import main, main_gateway
from sniffer.agent_app.contracts import MainIdentity
from sniffer.agent_app.main_gateway import MainGateway
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
