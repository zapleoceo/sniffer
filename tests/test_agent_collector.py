"""Finite worker orchestration and source-grounded extraction without paid calls."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.agent_app import collector, collector_gateway
from sniffer.agent_app.collector_gateway import CollectorGateway, Sessions
from sniffer.agent_app.contracts import CollectionScope
from sniffer.agent_app.extraction import Original, extract, observation
from sniffer.broker.client import BrokerCapError, BrokerResult
from sniffer.config import Settings
from sniffer.db.repositories.collection_tasks import CollectionLease, LeaseLost

NOW = datetime.now(UTC)
Storage = tuple[SimpleNamespace, Sessions, list[int]]
LEASE = CollectionLease(
    11,
    "trusted",
    {
        "city": "nha_trang",
        "category": "motorbike",
        "deal_type": "sell",
        "sources": ["chotot", "archive"],
    },
    1,
    NOW + timedelta(seconds=180),
)
SOURCE = Original(
    "chotot",
    "123",
    "https://www.chotot.com/123.htm",
    "Scooter",
    "Selling scooter in Nha Trang for 5000000 VND",
    NOW,
)
FACTS = {
    "facts": {
        "city": "nha_trang",
        "category": "motorbike",
        "deal_type": "sell",
        "price_vnd": 5000000,
        "active": True,
    },
    "evidence": [
        {"field": "city", "quote": "Nha Trang"},
        {"field": "category", "quote": "scooter"},
        {"field": "deal_type", "quote": "Selling"},
        {"field": "active", "quote": "Selling"},
        {"field": "price_vnd", "quote": "5000000 VND"},
    ],
}


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> Storage:
    repo = SimpleNamespace(
        claim=AsyncMock(return_value=LEASE),
        require_lease=AsyncMock(),
        complete=AsyncMock(),
        fail=AsyncMock(),
        heartbeat=AsyncMock(),
    )
    session = SimpleNamespace(commit=AsyncMock())
    active = []

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        active.append(1)
        try:
            yield cast(AsyncSession, session)
        finally:
            active.pop()

    monkeypatch.setattr(collector, "CollectionTaskRepository", lambda _: repo)
    monkeypatch.setattr(collector_gateway, "CollectionTaskRepository", lambda _: repo)
    return repo, sessions, active


async def test_empty_queue_never_calls_model(storage: Storage) -> None:
    repo, sessions, _ = storage
    repo.claim.return_value = None
    work = AsyncMock()
    assert await collector.Collector(sessions=sessions, work=work).tick() == 0
    work.assert_not_awaited()
    repo.complete.assert_not_awaited()


async def test_work_runs_outside_transaction_and_fenced_completion(storage: Storage) -> None:
    repo, sessions, active = storage

    async def work(lease: CollectionLease) -> dict[str, int]:
        assert not active
        assert lease == LEASE
        return {"published": 1}

    assert await collector.Collector(sessions=sessions, work=work).tick() == 1
    repo.complete.assert_awaited_once_with(11, "trusted", {"published": 1})
    repo.fail.assert_not_awaited()


@pytest.mark.parametrize("error", [RuntimeError("private"), BrokerCapError("cap")])
async def test_failures_retry_finitely_with_cap_deferred_to_utc_midnight(
    storage: Storage, error: Exception
) -> None:
    repo, sessions, _ = storage
    await collector.Collector(sessions=sessions, work=AsyncMock(side_effect=error)).tick()
    repo.complete.assert_not_awaited()
    args, kwargs = repo.fail.await_args
    assert args[:2] == (11, "trusted")
    assert args[2] == ("budget_cap" if isinstance(error, BrokerCapError) else "collection_failed")
    assert 1 <= kwargs["retry_seconds"] <= 86400


async def test_cancellation_never_marks_unknown_outcome_complete(storage: Storage) -> None:
    repo, sessions, _ = storage
    with pytest.raises(asyncio.CancelledError):
        await collector.Collector(
            sessions=sessions, work=AsyncMock(side_effect=asyncio.CancelledError)
        ).tick()
    repo.complete.assert_not_awaited()
    repo.fail.assert_not_awaited()


async def test_old_worker_does_not_finish_after_lease_loss(storage: Storage) -> None:
    repo, sessions, _ = storage
    repo.fail.side_effect = LeaseLost()
    await collector.Collector(sessions=sessions, work=AsyncMock(side_effect=LeaseLost())).tick()
    repo.complete.assert_not_awaited()


async def test_source_enum_limits_and_outside_transaction(storage: Storage) -> None:
    repo, sessions, active = storage

    async def fetch(source: str, scope: CollectionScope, limit: int) -> list[Original]:
        assert not active
        assert source == "chotot" and scope.city == "nha_trang" and limit == 6
        return [SOURCE]

    gateway = CollectorGateway(LEASE, sessions=sessions, fetch=fetch)
    with pytest.raises(ValueError):
        await gateway.call("sources_collect", {"source": "https://localhost"})
    repo.require_lease.assert_not_awaited()
    assert await gateway.call("sources_collect", {"source": "chotot"}) == {
        "source": "chotot",
        "count": 1,
    }
    with pytest.raises(ValueError):
        await gateway.call("sources_collect", {"source": "chotot"})
    assert gateway.originals == [SOURCE]


async def test_extraction_cannot_substitute_identity_or_inherit_search_city() -> None:
    broker = SimpleNamespace(structured=AsyncMock(return_value=FACTS))
    assert await extract(SOURCE, broker) == FACTS
    args, kwargs = broker.structured.await_args
    assert args == (SOURCE.text,)
    assert kwargs["capability"] == "chat:sales" and kwargs["max_tokens"] == 2048
    assert "scope" not in kwargs and "passport" not in kwargs
    assert observation(SOURCE, FACTS).external_id == SOURCE.external_id
    with pytest.raises(ValidationError):
        observation(SOURCE, {**FACTS, "url": "https://attacker.com/1"})
    with pytest.raises(ValidationError):
        observation(SOURCE, {**FACTS, "evidence": [{"field": "city", "quote": "Da Nang"}]})
    unknown = observation(SOURCE, {"facts": {}, "evidence": []})
    assert not unknown.publishable and unknown.facts.city is None


async def test_stage_is_source_indexed_fenced_and_rejects_bad_evidence_before_write(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, sessions, _ = storage
    repo = SimpleNamespace(stage=AsyncMock(return_value=20), publish=AsyncMock(return_value=True))
    monkeypatch.setattr(collector_gateway, "CatalogObservationRepository", lambda _: repo)
    gateway = CollectorGateway(LEASE, sessions=sessions, fetch=AsyncMock(return_value=[SOURCE]))
    await gateway.call("sources_collect", {"source": "chotot"})
    with pytest.raises(ValueError):
        await gateway.call("catalog_stage", {"index": 1, "extracted": FACTS})
    with pytest.raises(ValidationError):
        await gateway.call(
            "catalog_stage",
            {"index": 0, "extracted": {"facts": {"city": "da_nang"}, "evidence": []}},
        )
    repo.stage.assert_not_awaited()
    result = await gateway.call("catalog_stage", {"index": 0, "extracted": FACTS})
    assert result == {"observation_id": 20, "published": True}
    assert repo.stage.await_args.args[:2] == (11, "trusted")
    repo.publish.assert_awaited_once_with(11, "trusted", 20)
    with pytest.raises(ValueError):
        await gateway.call("catalog_stage", {"index": 0, "extracted": FACTS})
    assert repo.stage.await_count == 1


def test_collector_disabled_by_default_and_interval_cannot_be_aggressive() -> None:
    assert "AGENT_COLLECTOR_ENABLED" in collector.missing_settings(Settings.model_construct())
    with pytest.raises(ValidationError):
        Settings(agent_collector_interval_s=59)


async def test_cap_stops_claiming_other_tasks_in_same_process(storage: Storage) -> None:
    repo, sessions, _ = storage
    worker = collector.Collector(sessions=sessions, work=AsyncMock(side_effect=BrokerCapError()))
    assert await worker.tick() == 1
    assert await worker.tick() == 0
    assert repo.claim.await_count == 1


async def test_heartbeat_failure_cancels_work_and_blocks_completion(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sessions, _ = storage
    cancelled = False
    original_sleep = asyncio.sleep

    async def quick_sleep(seconds: float) -> None:
        await original_sleep(0.001 if seconds == 30 else seconds)

    async def work(lease: CollectionLease) -> dict[str, int]:
        nonlocal cancelled
        try:
            await original_sleep(10)
        finally:
            cancelled = True
        return {}

    monkeypatch.setattr("sniffer.agent_app.collector.asyncio.sleep", quick_sleep)
    repo.heartbeat.side_effect = LeaseLost()
    repo.fail.side_effect = LeaseLost()
    await collector.Collector(sessions=sessions, work=work).tick()
    assert cancelled
    repo.heartbeat.assert_awaited_once_with(11, "trusted", lease_seconds=90)
    repo.complete.assert_not_awaited()


async def test_deadline_cancels_work_and_records_finite_retry(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sessions, _ = storage
    timeout = asyncio.timeout
    monkeypatch.setattr("sniffer.agent_app.collector.asyncio.timeout", lambda _: timeout(0.001))

    async def work(lease: CollectionLease) -> dict[str, int]:
        await asyncio.sleep(10)
        return {}

    await collector.Collector(sessions=sessions, work=work).tick()
    repo.complete.assert_not_awaited()
    assert repo.fail.await_args.kwargs["retry_seconds"] == 3600


@pytest.mark.parametrize("tool_name", ["sources_collect", "catalog_stage"])
async def test_real_mcp_collector_loop_and_model_write_tool_denial(
    storage: Storage, monkeypatch: pytest.MonkeyPatch, tool_name: str
) -> None:
    _, sessions, _ = storage
    lease = CollectionLease(
        11, "trusted", {**LEASE.scope, "sources": ["chotot"]}, 1, LEASE.deadline_at
    )
    fetch = AsyncMock(return_value=[SOURCE])
    gateway = CollectorGateway(lease, sessions=sessions, fetch=fetch)
    repo = SimpleNamespace(
        stage=AsyncMock(return_value=20),
        publish=AsyncMock(return_value=True),
        record_coverage=AsyncMock(),
    )
    monkeypatch.setattr(collector, "CollectorGateway", lambda _: gateway)
    monkeypatch.setattr(collector, "session_scope", sessions)
    monkeypatch.setattr(collector, "CatalogObservationRepository", lambda _: repo)
    monkeypatch.setattr(collector_gateway, "CatalogObservationRepository", lambda _: repo)
    broker = SimpleNamespace(
        chat=AsyncMock(
            side_effect=[
                BrokerResult(
                    text="",
                    finish_reason="tool_calls",
                    tool_calls=[
                        {
                            "id": "one",
                            "type": "function",
                            "function": {"name": tool_name, "arguments": '{"source":"chotot"}'},
                        }
                    ],
                ),
                BrokerResult(text="done", finish_reason="stop"),
            ]
        ),
        structured=AsyncMock(return_value=FACTS),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(collector, "BrokerClient", lambda **_: broker)
    if tool_name == "catalog_stage":
        with pytest.raises(BaseExceptionGroup):
            await collector.process(lease)
        fetch.assert_not_awaited()
        repo.stage.assert_not_awaited()
        broker.structured.assert_not_awaited()
    else:
        assert await collector.process(lease) == {"collected": 1, "published": 1}
        assert broker.chat.await_count == 2 and broker.structured.await_count == 1
        repo.stage.assert_awaited_once()
        repo.publish.assert_awaited_once()
        repo.record_coverage.assert_awaited_once_with(11, "trusted", "chotot", "success")
    broker.aclose.assert_awaited_once()
