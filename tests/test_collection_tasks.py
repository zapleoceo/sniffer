"""Queue fault boundaries and real-Postgres ownership/recovery contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sniffer.db.collection_models import CollectionAction, CollectionTask
from sniffer.db.repositories.collection_tasks import (
    CollectionTaskRepository,
    LeaseLost,
    fingerprint,
)

SCOPE = {"city": "nha_trang", "category": "motorbike", "sources": ["chotot"]}


async def enqueue(repo: CollectionTaskRepository, *, user: int = 1, window: str = "hour1") -> int:
    return await repo.enqueue(
        SCOPE, user_id=user, request_id=user, request_version=1, window_key=window
    )


def test_canonical_scope_fingerprint() -> None:
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "x" * 16001])
def test_nonfinite_and_oversized_payload_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        fingerprint({"x": value})


@pytest.mark.parametrize("lease", [0, -1, True, 901])
async def test_invalid_claim_limit_does_not_touch_database(lease: int) -> None:
    session = AsyncMock(spec=AsyncSession)
    with pytest.raises(ValueError):
        await CollectionTaskRepository(session).claim(lease_seconds=lease)
    session.execute.assert_not_called()


async def test_dedup_and_private_status(db_session: AsyncSession) -> None:
    repo = CollectionTaskRepository(db_session)
    first = await enqueue(repo)
    assert await enqueue(repo, user=2) == first
    assert await enqueue(repo) == first
    assert await repo.status_for(99, 1, 1) == []
    assert await repo.status_for(1, 1, 2) == []
    assert len(await repo.status_for(1, 1, 1)) == 1
    assert await enqueue(repo, window="hour2") != first


async def test_claim_locks_skip_between_two_workers(db_engine: AsyncEngine) -> None:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as setup:
        repo = CollectionTaskRepository(setup)
        first = await enqueue(repo)
        second = await enqueue(repo, window="hour2")
        await setup.commit()
    async with sessions() as a, sessions() as b:
        lease_a = await CollectionTaskRepository(a).claim()
        lease_b = await CollectionTaskRepository(b).claim()
        assert lease_a is not None and lease_b is not None
        assert {lease_a.id, lease_b.id} == {first, second}
        assert lease_a.token != lease_b.token
        await a.commit()
        await b.commit()


async def test_restart_reclaims_and_fences_old_worker(db_session: AsyncSession) -> None:
    repo = CollectionTaskRepository(db_session)
    task = await enqueue(repo)
    old = await repo.claim()
    assert old is not None
    await db_session.commit()
    await db_session.execute(
        update(CollectionTask)
        .where(CollectionTask.id == task)
        .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db_session.commit()
    current = await repo.claim()
    assert current is not None and current.attempts == 2 and current.token != old.token
    with pytest.raises(LeaseLost):
        await repo.complete(task, old.token, {})
    with pytest.raises(LeaseLost):
        await repo.heartbeat(task, old.token)
    await repo.complete(task, current.token, {"count": 1})
    assert (await repo.status_for(1, 1, 1))[0]["status"] == "done"


async def test_final_crashed_attempt_is_retired(db_session: AsyncSession) -> None:
    repo = CollectionTaskRepository(db_session)
    task = await repo.enqueue(
        SCOPE, user_id=1, request_id=1, request_version=1, window_key="hour1", max_attempts=1
    )
    assert await repo.claim() is not None
    await db_session.execute(
        update(CollectionTask)
        .where(CollectionTask.id == task)
        .values(deadline_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    assert await repo.claim() is None
    state = (await repo.status_for(1, 1, 1))[0]
    assert state["status"] == "failed" and state["attempts"] == 1


async def test_cancel_one_subscriber_keeps_shared_work(db_session: AsyncSession) -> None:
    repo = CollectionTaskRepository(db_session)
    task = await enqueue(repo)
    await enqueue(repo, user=2)
    lease = await repo.claim()
    assert lease is not None
    await repo.unsubscribe(1, 1)
    await repo.require_lease(task, lease.token)
    assert await repo.status_for(1, 1, 1) == []
    assert (await repo.status_for(2, 2, 1))[0]["status"] == "running"
    await repo.unsubscribe(2, 2)
    with pytest.raises(LeaseLost):
        await repo.complete(task, lease.token, {})
    assert await enqueue(repo, user=2) == task
    assert (await repo.status_for(2, 2, 1))[0]["status"] == "pending"


async def test_action_journal_is_fenced_replayable_and_atomic(db_session: AsyncSession) -> None:
    repo = CollectionTaskRepository(db_session)
    task = await enqueue(repo)
    lease = await repo.claim()
    assert lease is not None
    await db_session.commit()
    await repo.record_action(task, lease.token, "publish:1", {"id": 1}, {"revision": 1})
    await db_session.rollback()
    assert await repo.action_result(task, lease.token, "publish:1", {"id": 1}) is None
    await repo.record_action(task, lease.token, "publish:1", {"id": 1}, {"revision": 1})
    await db_session.commit()
    await repo.record_action(task, lease.token, "publish:1", {"id": 1}, {"revision": 1})
    assert len((await db_session.scalars(select(CollectionAction))).all()) == 1
    with pytest.raises(ValueError, match="key_conflict"):
        await repo.action_result(task, lease.token, "publish:1", {"id": 2})
    await repo.unsubscribe(1, 1)
    with pytest.raises(LeaseLost):
        await repo.record_action(task, lease.token, "publish:2", {}, {})


async def test_heartbeat_does_not_extend_absolute_deadline(db_session: AsyncSession) -> None:
    repo = CollectionTaskRepository(db_session)
    await enqueue(repo)
    lease = await repo.claim(lease_seconds=2, max_run_seconds=60)
    assert lease is not None
    await repo.heartbeat(lease.id, lease.token, lease_seconds=900)
    row = await db_session.get(CollectionTask, lease.id)
    assert row is not None and row.lease_until == row.deadline_at == lease.deadline_at


async def test_fail_retries_are_finite(db_session: AsyncSession) -> None:
    repo = CollectionTaskRepository(db_session)
    await repo.enqueue(
        SCOPE, user_id=1, request_id=1, request_version=1, window_key="hour1", max_attempts=1
    )
    lease = await repo.claim()
    assert lease is not None
    await repo.fail(lease.id, lease.token, "source_unavailable")
    assert (await repo.status_for(1, 1, 1))[0]["status"] == "failed"
    assert await repo.claim() is None
