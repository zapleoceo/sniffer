"""Durable collection leases. Caller owns short transaction and explicit commit."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from sniffer.db import collection_models as _models  # noqa: F401 -- register ORM metadata
from sniffer.db.repositories.base import Repository


class LeaseLost(RuntimeError):
    """Expired/replaced/cancelled worker must not publish or finish."""


@dataclass(frozen=True, slots=True)
class CollectionLease:
    id: int
    token: str
    scope: dict[str, Any]
    attempts: int
    deadline_at: datetime
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class CollectionRecipient:
    user_id: int
    request_id: int
    request_version: int


@dataclass(frozen=True, slots=True)
class CollectionDeliveryState:
    task_id: int
    task_status: str
    attempts: int
    max_attempts: int
    error_code: str | None
    created_at: datetime
    user_id: int
    tg_user_id: int
    request_id: int
    request_version: int
    active: bool
    reply_queued_at: datetime | None
    outbox_id: int | None
    delivery_status: str | None
    delivery_attempts: int | None
    scheduled_at: datetime | None
    sent_at: datetime | None


def fingerprint(value: dict[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(payload) > 16000:
        raise ValueError("collection_payload_too_large")
    return hashlib.sha256(payload.encode()).hexdigest()


def _positive(value: int, maximum: int) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("invalid_collection_limit")


class CollectionTaskRepository(Repository):
    async def enqueue(
        self,
        scope: dict[str, Any],
        *,
        user_id: int,
        request_id: int,
        request_version: int,
        window_key: str,
        max_attempts: int = 3,
    ) -> int:
        """Same scope/window is shared; private subscribers never enter task scope."""
        for value in (user_id, request_id, request_version):
            _positive(value, 2**63 - 1)
        _positive(max_attempts, 10)
        if not window_key or len(window_key) > 100:
            raise ValueError("invalid_collection_window")
        dedup = fingerprint({"scope": scope, "window": window_key})
        result = await self._session.execute(
            text("""
            INSERT INTO collection_tasks(dedup_key,scope,max_attempts)
            VALUES (:key,CAST(:scope AS jsonb),:max_attempts)
            ON CONFLICT(dedup_key) DO UPDATE SET
                status=CASE WHEN collection_tasks.status='cancelled'
                    AND collection_tasks.attempts<collection_tasks.max_attempts
                    THEN 'pending' ELSE collection_tasks.status END
            RETURNING id
        """),
            {
                "key": dedup,
                "scope": json.dumps(scope, allow_nan=False),
                "max_attempts": max_attempts,
            },
        )
        task_id = int(result.scalar_one())
        await self._session.execute(
            text("""
            INSERT INTO collection_subscribers(task_id,user_id,request_id,request_version)
            VALUES (:task,:user,:request,:version)
            ON CONFLICT(task_id,user_id,request_id,request_version) DO UPDATE SET active=TRUE
        """),
            {"task": task_id, "user": user_id, "request": request_id, "version": request_version},
        )
        # The upsert above locks the task row. A request that attaches after a
        # previous terminal run must make that shared task runnable again.
        await self._session.execute(
            text("""
            UPDATE collection_tasks t SET status='pending',attempts=0,
                run_after=clock_timestamp(),lease_token=NULL,lease_until=NULL,
                deadline_at=NULL,error_code=NULL
            WHERE t.id=:task AND t.status IN ('done','failed','cancelled')
                AND EXISTS (
                    SELECT 1 FROM collection_subscribers s
                    JOIN passports p ON p.user_id=s.user_id
                        AND COALESCE(p.root_id,p.id)=s.request_id
                        AND p.version=s.request_version AND p.is_current
                    WHERE s.task_id=t.id AND s.active AND s.reply_queued_at IS NULL
                )
        """),
            {"task": task_id},
        )
        return task_id

    async def claim(
        self, *, lease_seconds: int = 120, max_run_seconds: int = 900
    ) -> CollectionLease | None:
        _positive(lease_seconds, 900)
        _positive(max_run_seconds, 3600)
        # A crashed final attempt may have died before its reply was durable.
        # Keep one collection attempt available until every current subscriber
        # owns an outbox row; otherwise `exhausted` below would silence it.
        await self._session.execute(
            text("""
            WITH unanswered AS (
                SELECT t.id FROM collection_tasks t
                WHERE t.attempts>=t.max_attempts
                    AND (t.error_code IS NULL OR t.error_code NOT LIKE 'reply_failed_%')
                    AND (t.status='pending' OR (t.status='running' AND
                        (t.lease_until<=clock_timestamp()
                            OR t.deadline_at<=clock_timestamp())))
                    AND EXISTS (
                        SELECT 1 FROM collection_subscribers s
                        JOIN passports p ON p.user_id=s.user_id
                            AND COALESCE(p.root_id,p.id)=s.request_id
                            AND p.version=s.request_version AND p.is_current
                        WHERE s.task_id=t.id AND s.active AND s.reply_queued_at IS NULL
                    )
                FOR UPDATE OF t SKIP LOCKED
            )
            UPDATE collection_tasks t SET status='pending',attempts=t.max_attempts-1,
                run_after=clock_timestamp(),lease_token=NULL,lease_until=NULL,
                deadline_at=NULL
            WHERE t.id IN (SELECT id FROM unanswered)
        """)
        )
        # Retire exhausted crashed attempts before selecting another runnable task.
        await self._session.execute(
            text("""
            WITH exhausted AS (
                SELECT id FROM collection_tasks WHERE attempts>=max_attempts AND
                    (error_code IS NULL OR error_code NOT LIKE 'reply_failed_%') AND
                    (status='pending' OR (status='running' AND
                    (lease_until<=clock_timestamp() OR deadline_at<=clock_timestamp())))
                FOR UPDATE SKIP LOCKED
            )
            UPDATE collection_tasks SET status='failed',lease_token=NULL,lease_until=NULL,
                error_code='attempts_exhausted'
            WHERE id IN (SELECT id FROM exhausted)
        """)
        )
        result = await self._session.execute(
            text("""
            WITH candidate AS (
                SELECT id FROM collection_tasks WHERE attempts<max_attempts AND
                    (error_code IS NULL OR error_code NOT LIKE 'reply_failed_%') AND
                    ((status='pending' AND run_after<=clock_timestamp()) OR
                     (status='running' AND (lease_until<=clock_timestamp()
                        OR deadline_at<=clock_timestamp())))
                ORDER BY run_after,id FOR UPDATE SKIP LOCKED LIMIT 1
            )
            UPDATE collection_tasks t SET status='running',attempts=attempts+1,
                lease_token=:token,
                lease_until=clock_timestamp()+make_interval(secs =>
                    LEAST(CAST(:lease AS double precision),CAST(:deadline AS double precision))),
                deadline_at=clock_timestamp()+make_interval(secs =>
                    CAST(:deadline AS double precision))
            FROM candidate WHERE t.id=candidate.id
            RETURNING t.id,t.lease_token,t.scope,t.attempts,t.deadline_at
        """),
            {"token": uuid4().hex, "lease": lease_seconds, "deadline": max_run_seconds},
        )
        row = result.mappings().first()
        return (
            None
            if row is None
            else CollectionLease(
                row["id"], row["lease_token"], row["scope"], row["attempts"], row["deadline_at"]
            )
        )

    async def claim_reply(
        self, *, lease_seconds: int = 90, max_run_seconds: int = 180
    ) -> CollectionLease | None:
        """Claim answer-only recovery without spending another collection attempt."""
        _positive(lease_seconds, 900)
        _positive(max_run_seconds, 3600)
        result = await self._session.execute(
            text("""
            WITH candidate AS (
                SELECT id FROM collection_tasks
                WHERE error_code LIKE 'reply_failed_%'
                    AND ((status='pending' AND run_after<=clock_timestamp()) OR
                        (status='running' AND (lease_until<=clock_timestamp()
                            OR deadline_at<=clock_timestamp())))
                ORDER BY run_after,id FOR UPDATE SKIP LOCKED LIMIT 1
            )
            UPDATE collection_tasks t SET status='running',lease_token=:token,
                lease_until=clock_timestamp()+make_interval(secs =>
                    LEAST(CAST(:lease AS double precision),CAST(:deadline AS double precision))),
                deadline_at=clock_timestamp()+make_interval(secs =>
                    CAST(:deadline AS double precision))
            FROM candidate WHERE t.id=candidate.id
            RETURNING t.id,t.lease_token,t.scope,t.attempts,t.deadline_at,t.error_code
        """),
            {"token": uuid4().hex, "lease": lease_seconds, "deadline": max_run_seconds},
        )
        row = result.mappings().first()
        return (
            None
            if row is None
            else CollectionLease(
                row["id"],
                row["lease_token"],
                row["scope"],
                row["attempts"],
                row["deadline_at"],
                row["error_code"],
            )
        )

    async def require_lease(self, task_id: int, lease_token: str) -> CollectionLease:
        # Check the clock AFTER acquiring the lock, not before a possible lock wait.
        await self._session.execute(
            text("SELECT id FROM collection_tasks WHERE id=:id FOR UPDATE"), {"id": task_id}
        )
        result = await self._session.execute(
            text("""
            SELECT id,lease_token,scope,attempts,deadline_at FROM collection_tasks
            WHERE id=:id AND status='running' AND lease_token=:token
              AND lease_until>clock_timestamp() AND deadline_at>clock_timestamp()
            FOR UPDATE
        """),
            {"id": task_id, "token": lease_token},
        )
        row = result.mappings().first()
        if row is None:
            raise LeaseLost("collection_lease_lost")
        return CollectionLease(
            row["id"], row["lease_token"], row["scope"], row["attempts"], row["deadline_at"]
        )

    async def heartbeat(self, task_id: int, lease_token: str, *, lease_seconds: int = 120) -> None:
        _positive(lease_seconds, 900)
        await self.require_lease(task_id, lease_token)
        await self._session.execute(
            text("""
            UPDATE collection_tasks SET lease_until=LEAST(deadline_at,
                clock_timestamp()+make_interval(secs => CAST(:lease AS double precision)))
                WHERE id=:id
        """),
            {"id": task_id, "lease": lease_seconds},
        )

    async def complete(self, task_id: int, lease_token: str, result: dict[str, Any]) -> None:
        fingerprint(result)
        await self.require_lease(task_id, lease_token)
        await self._session.execute(
            text("""
            WITH state AS (
                SELECT EXISTS (
                    SELECT 1 FROM collection_subscribers s
                    JOIN passports p ON p.user_id=s.user_id
                        AND COALESCE(p.root_id,p.id)=s.request_id
                        AND p.version=s.request_version AND p.is_current
                    WHERE s.task_id=:id AND s.active AND s.reply_queued_at IS NULL
                ) AS unanswered
            )
            UPDATE collection_tasks SET
                status=CASE WHEN state.unanswered THEN 'pending' ELSE 'done' END,
                attempts=CASE WHEN state.unanswered THEN 0 ELSE attempts END,
                run_after=CASE WHEN state.unanswered THEN clock_timestamp() ELSE run_after END,
                result=CAST(:result AS jsonb),lease_token=NULL,lease_until=NULL,
                deadline_at=NULL,error_code=NULL
            FROM state WHERE id=:id
        """),
            {"id": task_id, "result": json.dumps(result, allow_nan=False)},
        )

    async def fail(
        self, task_id: int, lease_token: str, error_code: str, *, retry_seconds: int = 300
    ) -> None:
        _positive(retry_seconds, 86400)
        if (
            not error_code
            or len(error_code) > 100
            or not all(c.isalnum() or c == "_" for c in error_code)
        ):
            raise ValueError("invalid_collection_error_code")
        await self.require_lease(task_id, lease_token)
        await self._session.execute(
            text("""
            WITH state AS (
                SELECT EXISTS (
                    SELECT 1 FROM collection_subscribers s
                    JOIN passports p ON p.user_id=s.user_id
                        AND COALESCE(p.root_id,p.id)=s.request_id
                        AND p.version=s.request_version AND p.is_current
                    WHERE s.task_id=:id AND s.active AND s.reply_queued_at IS NULL
                ) AS unanswered
            )
            UPDATE collection_tasks SET
                status=CASE WHEN attempts>=max_attempts AND NOT state.unanswered
                    THEN 'failed' ELSE 'pending' END,
                attempts=CASE WHEN attempts>=max_attempts AND state.unanswered
                    AND :error NOT LIKE 'reply_failed_%'
                    THEN max_attempts-1 ELSE attempts END,
                error_code=:error,lease_token=NULL,lease_until=NULL,deadline_at=NULL,
                run_after=clock_timestamp()+make_interval(secs => CAST(:retry AS double precision))
            FROM state WHERE id=:id
        """),
            {"id": task_id, "error": error_code, "retry": retry_seconds},
        )

    async def release_after_reply(
        self,
        task_id: int,
        lease_token: str,
        error_code: str,
        *,
        retry_seconds: int,
    ) -> None:
        """Finish answer-only recovery, then restore bounded collection retry."""
        _positive(retry_seconds, 86400)
        if not error_code or len(error_code) > 100:
            raise ValueError("invalid_collection_error_code")
        await self.require_lease(task_id, lease_token)
        result = await self._session.execute(
            text("""
            UPDATE collection_tasks t SET
                status=CASE WHEN attempts>=max_attempts THEN 'failed' ELSE 'pending' END,
                error_code=:error,lease_token=NULL,lease_until=NULL,deadline_at=NULL,
                run_after=clock_timestamp()+make_interval(secs => CAST(:retry AS double precision))
            WHERE id=:id AND NOT EXISTS (
                SELECT 1 FROM collection_subscribers s
                JOIN passports p ON p.user_id=s.user_id
                    AND COALESCE(p.root_id,p.id)=s.request_id
                    AND p.version=s.request_version AND p.is_current
                WHERE s.task_id=t.id AND s.active AND s.reply_queued_at IS NULL
            )
            RETURNING id
        """),
            {"id": task_id, "error": error_code, "retry": retry_seconds},
        )
        if result.scalar_one_or_none() is None:
            raise LeaseLost("collection_reply_not_durable")

    async def unsubscribe(self, user_id: int, request_id: int) -> None:
        # Lock tasks first, same order as enqueue, then subscribers; concurrent attach
        # cannot slip between the last-subscriber check and cancellation.
        rows = await self._session.execute(
            text("""
            SELECT id FROM collection_tasks WHERE id IN (SELECT task_id FROM
                collection_subscribers WHERE user_id=:user AND request_id=:request)
            ORDER BY id FOR UPDATE
        """),
            {"user": user_id, "request": request_id},
        )
        ids = list(rows.scalars())
        await self._session.execute(
            text("""
            UPDATE collection_subscribers SET active=FALSE
            WHERE user_id=:user AND request_id=:request
        """),
            {"user": user_id, "request": request_id},
        )
        for task_id in ids:
            await self._session.execute(
                text("""
                UPDATE collection_tasks SET status='cancelled',lease_token=NULL,lease_until=NULL
                WHERE id=:id AND status IN ('pending','running') AND NOT EXISTS
                    (SELECT 1 FROM collection_subscribers WHERE task_id=:id AND active)
            """),
                {"id": task_id},
            )

    async def status_for(
        self, user_id: int, request_id: int, request_version: int
    ) -> list[dict[str, Any]]:
        result = await self._session.execute(
            text("""
            SELECT t.id,t.status,t.attempts,t.error_code,t.result FROM collection_tasks t
            JOIN collection_subscribers s ON s.task_id=t.id
            WHERE s.user_id=:user AND s.request_id=:request
                AND s.request_version=:version AND s.active
            ORDER BY t.id DESC LIMIT 20
        """),
            {"user": user_id, "request": request_id, "version": request_version},
        )
        return [dict(row) for row in result.mappings()]

    async def recent_deliveries(self, *, limit: int = 20) -> list[CollectionDeliveryState]:
        """Recent collection tasks with their client-delivery outcome."""
        _positive(limit, 100)
        result = await self._session.execute(
            text("""
            SELECT t.id AS task_id,t.status AS task_status,t.attempts,t.max_attempts,
                t.error_code,t.created_at,s.user_id,u.tg_user_id,s.request_id,
                s.request_version,s.active,s.reply_queued_at,o.id AS outbox_id,
                o.status AS delivery_status,o.attempts AS delivery_attempts,
                o.scheduled_at,o.sent_at
            FROM collection_tasks t
            JOIN collection_subscribers s ON s.task_id=t.id
            JOIN users u ON u.id=s.user_id
            LEFT JOIN LATERAL (
                SELECT candidate.id,candidate.status,candidate.attempts,
                    candidate.scheduled_at,candidate.sent_at
                FROM outbox candidate
                WHERE candidate.user_id=s.user_id
                    AND candidate.payload->>'kind'='collection_result'
                    AND (
                        candidate.payload->>'collection_task_id'=t.id::text
                        OR (
                            candidate.payload->>'collection_task_id' IS NULL
                            AND s.reply_queued_at IS NOT NULL
                            AND candidate.scheduled_at>=s.reply_queued_at-interval '1 second'
                            AND candidate.scheduled_at<=s.reply_queued_at+interval '5 seconds'
                        )
                    )
                ORDER BY candidate.id DESC LIMIT 1
            ) o ON TRUE
            ORDER BY t.id DESC,s.user_id,s.request_id LIMIT :limit
        """),
            {"limit": limit},
        )
        return [CollectionDeliveryState(**dict(row)) for row in result.mappings()]

    async def pending_recipients(self, task_id: int, lease_token: str) -> list[CollectionRecipient]:
        """Current request versions still waiting for their deferred answer."""
        await self.require_lease(task_id, lease_token)
        result = await self._session.execute(
            text("""
            SELECT s.user_id,s.request_id,s.request_version
            FROM collection_subscribers s
            JOIN passports p ON p.user_id=s.user_id
                AND COALESCE(p.root_id,p.id)=s.request_id
                AND p.version=s.request_version AND p.is_current
            WHERE s.task_id=:task AND s.active AND s.reply_queued_at IS NULL
            ORDER BY s.user_id,s.request_id,s.request_version
        """),
            {"task": task_id},
        )
        return [CollectionRecipient(**dict(row)) for row in result.mappings()]

    async def queue_reply(
        self,
        task_id: int,
        lease_token: str,
        recipient: CollectionRecipient,
        payload: dict[str, Any],
    ) -> bool:
        """Atomically claim one subscriber reply and put it in the durable outbox."""
        await self.require_lease(task_id, lease_token)
        encoded = json.dumps(payload, allow_nan=False)
        if len(encoded) > 50000:
            raise ValueError("collection_reply_too_large")
        result = await self._session.execute(
            text("""
            WITH claimed AS (
                UPDATE collection_subscribers SET reply_queued_at=clock_timestamp()
                WHERE task_id=:task AND user_id=:user AND request_id=:request
                    AND request_version=:version AND active AND reply_queued_at IS NULL
                    AND EXISTS (
                        SELECT 1 FROM passports p WHERE p.user_id=:user
                            AND COALESCE(p.root_id,p.id)=:request
                            AND p.version=:version AND p.is_current
                    )
                RETURNING user_id
            )
            INSERT INTO outbox(user_id,payload)
            SELECT user_id,CAST(:payload AS jsonb) FROM claimed
            RETURNING id
        """),
            {
                "task": task_id,
                "user": recipient.user_id,
                "request": recipient.request_id,
                "version": recipient.request_version,
                "payload": encoded,
            },
        )
        return result.scalar_one_or_none() is not None

    async def action_result(
        self, task_id: int, lease_token: str, action_key: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        await self.require_lease(task_id, lease_token)
        result = await self._session.execute(
            text("""
            SELECT arguments_hash,result FROM collection_actions
            WHERE task_id=:id AND action_key=:key
        """),
            {"id": task_id, "key": action_key},
        )
        row = result.mappings().first()
        if row is None:
            return None
        if row["arguments_hash"] != fingerprint(arguments):
            raise ValueError("collection_action_key_conflict")
        return dict(row["result"])

    async def record_action(
        self,
        task_id: int,
        lease_token: str,
        action_key: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        if not action_key or len(action_key) > 128:
            raise ValueError("invalid_collection_action_key")
        fingerprint(result)
        previous = await self.action_result(task_id, lease_token, action_key, arguments)
        if previous is not None:
            if previous != result:
                raise ValueError("collection_action_result_conflict")
            return
        await self._session.execute(
            text("""
            INSERT INTO collection_actions(task_id,action_key,arguments_hash,result)
            VALUES (:id,:key,:hash,CAST(:result AS jsonb))
        """),
            {
                "id": task_id,
                "key": action_key,
                "hash": fingerprint(arguments),
                "result": json.dumps(result, allow_nan=False),
            },
        )
