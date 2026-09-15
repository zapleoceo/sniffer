"""Queue fault boundaries and real-Postgres ownership/recovery contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sniffer.db import models
from sniffer.db.collection_models import CollectionAction, CollectionTask
from sniffer.db.repositories import PassportRepository, UserRepository
from sniffer.db.repositories.collection_tasks import (
    CollectionRecipient,
    CollectionTaskRepository,
    LeaseLost,
    fingerprint,
)
from sniffer.domain.passport import Category, Intent, Passport

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


async def test_deferred_reply_is_current_owned_and_exactly_once(db_session: AsyncSession) -> None:
    user = await UserRepository(db_session).get_or_create(919191)
    assert user.id is not None
    passport = await PassportRepository(db_session).save_new(
        user.id,
        Passport(intent=Intent.RENT, category=Category.MOTORBIKE, city="nha_trang"),
    )
    repo = CollectionTaskRepository(db_session)
    task = await repo.enqueue(
        SCOPE,
        user_id=user.id,
        request_id=passport.root,
        request_version=passport.version,
        window_key="reply",
    )
    lease = await repo.claim()
    assert lease is not None and lease.id == task
    recipient = CollectionRecipient(user.id, passport.root, passport.version)

    assert await repo.pending_recipients(task, lease.token) == [recipient]
    payload = {"kind": "collection_result", "collection_task_id": task}
    assert await repo.queue_reply(task, lease.token, recipient, payload)
    assert not await repo.queue_reply(task, lease.token, recipient, payload)
    assert await repo.pending_recipients(task, lease.token) == []
    assert len((await db_session.scalars(select(models.Outbox))).all()) == 1
    delivery = (await repo.recent_deliveries())[0]
    assert delivery.task_id == task
    assert delivery.tg_user_id == 919191
    assert delivery.delivery_status == "pending"


async def test_late_subscriber_keeps_completed_task_runnable(db_session: AsyncSession) -> None:
    first = await UserRepository(db_session).get_or_create(111001)
    second = await UserRepository(db_session).get_or_create(111002)
    assert first.id is not None and second.id is not None
    first_passport = await PassportRepository(db_session).save_new(
        first.id, Passport(intent=Intent.RENT, category=Category.MOTORBIKE, city="nha_trang")
    )
    second_passport = await PassportRepository(db_session).save_new(
        second.id, Passport(intent=Intent.RENT, category=Category.MOTORBIKE, city="nha_trang")
    )
    repo = CollectionTaskRepository(db_session)
    task = await repo.enqueue(
        SCOPE,
        user_id=first.id,
        request_id=first_passport.root,
        request_version=1,
        window_key="late",
    )
    lease = await repo.claim()
    assert lease is not None
    first_recipient = CollectionRecipient(first.id, first_passport.root, 1)
    assert await repo.queue_reply(
        task,
        lease.token,
        first_recipient,
        {"kind": "collection_result", "collection_task_id": task},
    )

    assert (
        await repo.enqueue(
            SCOPE,
            user_id=second.id,
            request_id=second_passport.root,
            request_version=1,
            window_key="late",
        )
        == task
    )
    await repo.complete(task, lease.token, {"answers_queued": 1})

    state = (await repo.status_for(second.id, second_passport.root, 1))[0]
    assert state["status"] == "pending"
    next_lease = await repo.claim()
    assert next_lease is not None and next_lease.id == task and next_lease.attempts == 1


async def test_new_request_reopens_terminal_shared_task(db_session: AsyncSession) -> None:
    first = await UserRepository(db_session).get_or_create(112001)
    second = await UserRepository(db_session).get_or_create(112002)
    assert first.id is not None and second.id is not None
    first_passport = await PassportRepository(db_session).save_new(
        first.id, Passport(intent=Intent.RENT, category=Category.MOTORBIKE, city="nha_trang")
    )
    second_passport = await PassportRepository(db_session).save_new(
        second.id, Passport(intent=Intent.RENT, category=Category.MOTORBIKE, city="nha_trang")
    )
    repo = CollectionTaskRepository(db_session)
    task = await repo.enqueue(
        SCOPE,
        user_id=first.id,
        request_id=first_passport.root,
        request_version=1,
        window_key="reopen",
    )
    lease = await repo.claim()
    assert lease is not None
    await repo.queue_reply(
        task,
        lease.token,
        CollectionRecipient(first.id, first_passport.root, 1),
        {"kind": "collection_result", "collection_task_id": task},
    )
    await repo.complete(task, lease.token, {})
    assert (await repo.status_for(first.id, first_passport.root, 1))[0]["status"] == "done"

    reopened = await repo.enqueue(
        SCOPE,
        user_id=second.id,
        request_id=second_passport.root,
        request_version=1,
        window_key="reopen",
    )
    assert reopened == task
    assert (await repo.status_for(second.id, second_passport.root, 1))[0]["status"] == "pending"


async def test_exhausted_task_cannot_fail_while_current_reply_is_missing(
    db_session: AsyncSession,
) -> None:
    user = await UserRepository(db_session).get_or_create(113001)
    assert user.id is not None
    passport = await PassportRepository(db_session).save_new(
        user.id, Passport(intent=Intent.RENT, category=Category.MOTORBIKE, city="nha_trang")
    )
    repo = CollectionTaskRepository(db_session)
    await repo.enqueue(
        SCOPE,
        user_id=user.id,
        request_id=passport.root,
        request_version=1,
        window_key="unanswered",
        max_attempts=1,
    )
    lease = await repo.claim()
    assert lease is not None
    await repo.fail(lease.id, lease.token, "source_unavailable", retry_seconds=1)

    state = (await repo.status_for(user.id, passport.root, 1))[0]
    assert state["status"] == "pending" and state["attempts"] == 0


async def test_reply_only_recovery_does_not_repeat_or_consume_collection_attempt(
    db_session: AsyncSession,
) -> None:
    user = await UserRepository(db_session).get_or_create(114001)
    assert user.id is not None
    passport = await PassportRepository(db_session).save_new(
        user.id, Passport(intent=Intent.RENT, category=Category.MOTORBIKE, city="nha_trang")
    )
    repo = CollectionTaskRepository(db_session)
    task = await repo.enqueue(
        SCOPE,
        user_id=user.id,
        request_id=passport.root,
        request_version=1,
        window_key="reply-only",
        max_attempts=1,
    )
    lease = await repo.claim()
    assert lease is not None and lease.attempts == 1
    await repo.fail(
        task,
        lease.token,
        "reply_failed_budget_cap",
        retry_seconds=1,
    )
    await db_session.execute(
        update(CollectionTask)
        .where(CollectionTask.id == task)
        .values(run_after=datetime.now(UTC) - timedelta(seconds=1))
    )

    reply_lease = await repo.claim_reply()
    assert reply_lease is not None
    assert reply_lease.id == task and reply_lease.attempts == 1
    assert reply_lease.error_code == "reply_failed_budget_cap"
    recipient = CollectionRecipient(user.id, passport.root, 1)
    assert await repo.queue_reply(
        task,
        reply_lease.token,
        recipient,
        {"kind": "collection_result", "collection_task_id": task},
    )
    await repo.release_after_reply(task, reply_lease.token, "budget_cap", retry_seconds=1)
    state = (await repo.status_for(user.id, passport.root, 1))[0]
    assert state["status"] == "failed" and state["attempts"] == 1
