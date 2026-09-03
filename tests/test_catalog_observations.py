"""Evidence, fencing and real Postgres monotonic updates for the new catalog."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db.repositories.catalog_observations import (
    CatalogObservationRepository,
    CatalogRejected,
)
from sniffer.db.repositories.collection_tasks import CollectionTaskRepository, LeaseLost
from sniffer.domain.catalog import CatalogFacts, CatalogObservation, Evidence
from sniffer.domain.passport import Category


def observation(
    *, price: int = 100, city: str | None = "nha_trang", **kwargs: Any
) -> CatalogObservation:
    raw = f"Нячанг. Продам скутер. Цена {price} VND. В наличии."
    facts = CatalogFacts(
        city=city, category=Category.MOTORBIKE, deal_type="sell", price_vnd=price, active=True
    )
    quotes = {
        "city": "Нячанг",
        "category": "скутер",
        "deal_type": "Продам",
        "price_vnd": str(price),
        "active": "В наличии",
    }
    data: dict[str, Any] = {
        "source": "chotot",
        "external_id": "42",
        "url": "https://www.chotot.com/42.htm",
        "fetched_at": datetime.now(UTC) - timedelta(seconds=30),
        "title": "Скутер",
        "raw_text": raw,
        "extractor_version": "test-v1",
        "facts": facts,
        "evidence": tuple(
            Evidence.model_validate({"field": field, "quote": quote})
            for field, quote in quotes.items()
            if field != "city" or city is not None
        ),
    }
    data.update(kwargs)
    return CatalogObservation(**data)


def test_unknown_city_stays_unknown_and_unpublishable() -> None:
    record = observation(city=None)
    assert record.facts.city is None
    assert not record.publishable


async def test_publication_rejects_deal_type_outside_task_scope() -> None:
    session = AsyncMock(spec=AsyncSession)
    repo = CatalogObservationRepository(session)
    request = observation()
    query = MagicMock()
    query.mappings.return_value.one_or_none.return_value = {
        "payload": request.model_dump(mode="json")
    }
    session.execute.return_value = query
    lease = MagicMock()
    lease.scope = {
        "city": "nha_trang",
        "category": "motorbike",
        "deal_type": "rent_out",
        "sources": ["chotot"],
    }
    with pytest.MonkeyPatch.context() as patch:
        check = AsyncMock(return_value=lease)
        patch.setattr(
            "sniffer.db.repositories.catalog_observations.CollectionTaskRepository.require_lease",
            check,
        )
        with pytest.raises(CatalogRejected, match="facts_outside_task_scope"):
            await repo.publish(1, "token", 2)


@pytest.mark.parametrize(
    "field,value",
    [
        ("url", "https://localhost/42"),
        ("url", "https://www.chotot.com.attacker.test/42"),
        ("url", "https://u:p@chotot.com/42"),
        ("url", "http://chotot.com/42"),
        ("url", "https://chotot.com:8443/42"),
        ("url", "https://t.me/42"),
        ("source", "untrusted"),
        ("fetched_at", datetime(2026, 9, 1)),
        ("posted_at", datetime.now(UTC) + timedelta(days=1)),
        ("raw_text", "no supporting evidence"),
        ("evidence", ()),
    ],
)
def test_invalid_evidence_and_source_metadata_rejected(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        observation(**{field: value})


def test_duplicate_and_unsupported_facts_rejected() -> None:
    record = observation()
    with pytest.raises(ValidationError):
        observation(evidence=(*record.evidence, record.evidence[0]))
    with pytest.raises(ValidationError):
        CatalogFacts(price_vnd=True)
    with pytest.raises(ValidationError):
        CatalogFacts(price_vnd=-1)
    with pytest.raises(ValidationError):
        CatalogFacts.model_validate({"sql": "DROP TABLE listings"})


async def test_lost_lease_has_zero_catalog_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    require = AsyncMock(side_effect=LeaseLost("lease_lost"))
    monkeypatch.setattr(CollectionTaskRepository, "require_lease", require)
    session = AsyncMock(spec=AsyncSession)
    repo = CatalogObservationRepository(session)
    for call in (
        repo.stage(1, "old", observation()),
        repo.publish(1, "old", 2),
        repo.record_coverage(1, "old", "chotot", "success"),
    ):
        with pytest.raises(LeaseLost):
            await call
    session.execute.assert_not_called()
    session.scalar.assert_not_called()


@pytest.mark.parametrize(
    "bounds",
    [
        {"limit": True},
        {"limit": 51},
        {"fresh_seconds": 0},
        {"fresh_seconds": True},
        {"max_price_vnd": True},
        {"max_price_vnd": -1},
    ],
)
async def test_invalid_search_bounds_do_not_reach_database(bounds: dict[str, Any]) -> None:
    session = AsyncMock(spec=AsyncSession)
    with pytest.raises(CatalogRejected):
        await CatalogObservationRepository(session).search(
            city="nha_trang", category="motorbike", **bounds
        )
    session.execute.assert_not_called()


async def test_database_updates_price_without_duplicate_and_late_replay(
    db_session: AsyncSession,
) -> None:
    tasks = CollectionTaskRepository(db_session)
    task_id = await tasks.enqueue(
        {"city": "nha_trang", "category": "motorbike", "sources": ["chotot"]},
        user_id=1,
        request_id=1,
        request_version=1,
        window_key="hour",
    )
    lease = await tasks.claim()
    assert lease is not None and lease.id == task_id
    repo = CatalogObservationRepository(db_session)
    initial = observation()
    first = await repo.stage(task_id, lease.token, initial)
    assert await repo.stage(task_id, lease.token, initial) == first
    assert await repo.publish(task_id, lease.token, first)
    changed = observation(price=200, fetched_at=initial.fetched_at + timedelta(seconds=1))
    second = await repo.stage(task_id, lease.token, changed)
    assert second != first
    assert await repo.publish(task_id, lease.token, second)
    assert not await repo.publish(task_id, lease.token, first)
    rows = await repo.search(city="nha_trang", category="motorbike")
    assert len(rows) == 1 and rows[0]["observation"]["facts"]["price_vnd"] == 200
    assert await repo.search(city="nha_trang", category="motorbike", max_price_vnd=150) == []
    await repo.record_coverage(task_id, lease.token, "chotot", "success")
    assert await repo.coverage(lease.scope) == {"sources": {"chotot": "fresh"}}
    await repo.record_coverage(task_id, lease.token, "chotot", "error")
    assert await repo.coverage(lease.scope) == {"sources": {"chotot": "error"}}
    assert len(await repo.search(city="nha_trang", category="motorbike")) == 1


async def test_unknown_city_foreign_task_and_scope_cannot_publish(db_session: AsyncSession) -> None:
    tasks = CollectionTaskRepository(db_session)
    task_id = await tasks.enqueue(
        {"city": "nha_trang", "category": "motorbike", "sources": ["chotot"]},
        user_id=1,
        request_id=1,
        request_version=1,
        window_key="hour",
    )
    lease = await tasks.claim()
    assert lease is not None
    repo = CatalogObservationRepository(db_session)
    unknown = await repo.stage(task_id, lease.token, observation(city=None))
    assert not await repo.publish(task_id, lease.token, unknown)
    wrong_city = await repo.stage(task_id, lease.token, observation(city="da_nang"))
    with pytest.raises(CatalogRejected, match="facts_outside_task_scope"):
        await repo.publish(task_id, lease.token, wrong_city)
    with pytest.raises(CatalogRejected, match="observation_not_in_task"):
        await repo.publish(task_id, lease.token, unknown + 1000)
    with pytest.raises(CatalogRejected, match="source_outside_task_scope"):
        await repo.stage(
            task_id,
            lease.token,
            observation(source="telegram_archive", url="https://t.me/group/42"),
        )
    assert await repo.search(city="nha_trang", category="motorbike") == []


async def test_stale_future_and_removed_records_are_not_served(db_session: AsyncSession) -> None:
    tasks = CollectionTaskRepository(db_session)
    task_id = await tasks.enqueue(
        {"city": "nha_trang", "category": "motorbike", "sources": ["chotot"]},
        user_id=1,
        request_id=1,
        request_version=1,
        window_key="hour",
    )
    lease = await tasks.claim()
    assert lease is not None
    repo = CatalogObservationRepository(db_session)
    old = await repo.stage(
        task_id, lease.token, observation(fetched_at=datetime.now(UTC) - timedelta(days=2))
    )
    assert await repo.publish(task_id, lease.token, old)
    assert await repo.search(city="nha_trang", category="motorbike") == []
    # A permitted small clock skew in staging must not create fresh future cards.
    future = await repo.stage(
        task_id,
        lease.token,
        observation(external_id="future", fetched_at=datetime.now(UTC) + timedelta(minutes=1)),
    )
    assert await repo.publish(task_id, lease.token, future)
    assert await repo.search(city="nha_trang", category="motorbike") == []
    record = observation()
    removed = record.model_copy(
        update={
            "facts": record.facts.model_copy(update={"active": False}),
            "raw_text": record.raw_text + " Продано.",
            "evidence": (
                *(e for e in record.evidence if e.field != "active"),
                Evidence(field="active", quote="Продано"),
            ),
        }
    )
    identifier = await repo.stage(task_id, lease.token, removed)
    assert await repo.publish(task_id, lease.token, identifier)
    assert await repo.search(city="nha_trang", category="motorbike") == []
