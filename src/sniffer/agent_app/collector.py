"""Hourly bounded collection worker; leases and publication use short transactions."""

from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial

import structlog

from sniffer.agent_app.collector_gateway import Sessions
from sniffer.agent_app.followup import queue_answers, queue_cap_answers, queue_failure_answers
from sniffer.agent_app.scenario_router import process as process
from sniffer.agent_app.scenario_router import require_available
from sniffer.bot.quota import QuotaService
from sniffer.bot.quota_ledger import new_quota
from sniffer.broker.client import BrokerCapError
from sniffer.config import Settings, get_settings
from sniffer.db.engine import session_scope
from sniffer.db.repositories.collection_tasks import (
    CollectionLease,
    CollectionTaskRepository,
    LeaseLost,
)
from sniffer.domain.scenarios import ScenarioUnavailable
from sniffer.runtime.service import Service, run_service, sleep_or_stop

log = structlog.get_logger(__name__)
Process = Callable[[CollectionLease], Awaitable[dict[str, int]]]
Reply = Callable[[CollectionLease], Awaitable[int]]


class Collector:
    def __init__(
        self,
        *,
        sessions: Sessions = session_scope,
        work: Process = process,
        reply: Reply | None = None,
        failure_reply: Reply | None = None,
        cap_reply: Reply | None = None,
        quota: QuotaService | None = None,
    ) -> None:
        self._sessions, self._work = sessions, work
        # With a quota every recipient is shown only what their allowance lets them see; the
        # cap reply carries no cards, so there is nothing to meter there.
        self._reply = reply or (partial(queue_answers, quota=quota) if quota else queue_answers)
        self._failure_reply = failure_reply or (
            partial(queue_failure_answers, quota=quota) if quota else queue_failure_answers
        )
        self._cap_reply = cap_reply or queue_cap_answers
        self._capped_until: datetime | None = None

    async def tick(self) -> int:
        capped = self._capped_until is not None and datetime.now(UTC) < self._capped_until
        async with self._sessions() as session:
            repo = CollectionTaskRepository(session)
            lease = await repo.claim_reply(lease_seconds=90, max_run_seconds=180)
            reply_only = lease is not None
            if lease is None and not capped:
                lease = await repo.claim(lease_seconds=90, max_run_seconds=180)
            await session.commit()
        if lease is None:
            return 0
        try:
            require_available(lease.scope)
        except ScenarioUnavailable as exc:
            await self._fail(lease, str(exc), delay=86400)
            return 1
        if reply_only:
            await self.retry_failure_reply(lease)
            return 1
        try:
            async with asyncio.timeout(180):
                result = await self._with_heartbeat(lease)
            async with self._sessions() as session:
                await CollectionTaskRepository(session).complete(lease.id, lease.token, result)
                await session.commit()
        except asyncio.CancelledError:
            # Shutdown never finishes an unknown outcome. Lease recovery owns retry.
            raise
        except Exception as exc:
            unavailable = _scenario_unavailable(exc)
            if unavailable is not None:
                # A disabled workflow must never invoke marketplace failure replies.
                error_code, delay = str(unavailable), 86400
            else:
                try:
                    async with asyncio.timeout(70):
                        reply = self._cap_reply if _has_cap(exc) else self._failure_reply
                        await reply(lease)
                except Exception as reply_exc:
                    log.exception(
                        "collector.failure_reply_failed",
                        task_id=lease.id,
                        kind=type(reply_exc).__name__,
                    )
                    error_code = (
                        "reply_failed_budget_cap"
                        if _has_cap(exc)
                        else "reply_failed_collection_failed"
                    )
                    delay = 300
                else:
                    error_code = "budget_cap" if _has_cap(exc) else "collection_failed"
                    delay = _cap_delay() if _has_cap(exc) else 3600
                if _has_cap(exc):
                    self._capped_until = datetime.now(UTC) + timedelta(seconds=_cap_delay())
            await self._fail(lease, error_code, delay=delay)
        return 1

    async def _fail(self, lease: CollectionLease, error_code: str, *, delay: int) -> None:
        async with self._sessions() as session:
            try:
                await CollectionTaskRepository(session).fail(
                    lease.id, lease.token, error_code, retry_seconds=delay
                )
                await session.commit()
            except LeaseLost:
                log.info("collector.lease_lost", task_id=lease.id)

    async def retry_failure_reply(self, lease: CollectionLease) -> None:
        error_code = lease.error_code or "reply_failed_collection_failed"
        original_error = "budget_cap" if error_code.endswith("budget_cap") else "collection_failed"
        try:
            async with asyncio.timeout(70):
                reply = self._cap_reply if original_error == "budget_cap" else self._failure_reply
                await reply(lease)
        except Exception as exc:
            log.exception(
                "collector.failure_reply_failed", task_id=lease.id, kind=type(exc).__name__
            )
            async with self._sessions() as session:
                try:
                    await CollectionTaskRepository(session).fail(
                        lease.id, lease.token, error_code, retry_seconds=300
                    )
                    await session.commit()
                except LeaseLost:
                    log.info("collector.lease_lost", task_id=lease.id)
            return
        delay = _cap_delay() if original_error == "budget_cap" else 3600
        async with self._sessions() as session:
            try:
                await CollectionTaskRepository(session).release_after_reply(
                    lease.id, lease.token, original_error, retry_seconds=delay
                )
                await session.commit()
            except LeaseLost:
                log.info("collector.lease_lost", task_id=lease.id)

    async def _with_heartbeat(self, lease: CollectionLease) -> dict[str, int]:
        async def execute() -> dict[str, int]:
            result = await self._work(lease)
            result["answers_queued"] = await self._reply(lease)
            return result

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(30)
                async with self._sessions() as session:
                    await CollectionTaskRepository(session).heartbeat(
                        lease.id, lease.token, lease_seconds=90
                    )
                    await session.commit()

        async with asyncio.TaskGroup() as group:
            pulse = group.create_task(heartbeat())
            work = group.create_task(execute())
            try:
                result = await work
            finally:
                pulse.cancel()
        return result


def _has_cap(exc: BaseException) -> bool:
    return isinstance(exc, BrokerCapError) or (
        isinstance(exc, BaseExceptionGroup) and any(_has_cap(child) for child in exc.exceptions)
    )


def _scenario_unavailable(exc: BaseException) -> ScenarioUnavailable | None:
    if isinstance(exc, ScenarioUnavailable):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        return next(
            (
                found
                for child in exc.exceptions
                if (found := _scenario_unavailable(child)) is not None
            ),
            None,
        )
    return None


def _cap_delay() -> int:
    now = datetime.now(UTC)
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    return max(1, min(86400, int((midnight - now).total_seconds()) + 1))


def missing_settings(settings: Settings) -> list[str]:
    return (["AGENT_COLLECTOR_ENABLED"] if not settings.agent_collector_enabled else []) + (
        ["BROKER_PROJECT_KEY"] if not settings.broker_project_key else []
    )


async def run(stop: asyncio.Event) -> None:
    collector = Collector(quota=new_quota())
    while not stop.is_set():
        started = time.monotonic()
        # Each hourly pass has a hard finite task budget; no hot-loop on retries.
        for _ in range(10):
            if stop.is_set() or not await collector.tick():
                break
        remaining = get_settings().agent_collector_interval_s - (time.monotonic() - started)
        await sleep_or_stop(stop, max(0, remaining))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.once:
        if missing_settings(get_settings()):
            raise SystemExit("collector_disabled_or_unconfigured")
        asyncio.run(Collector(quota=new_quota()).tick())
    else:
        run_service(Service("agent-collector", missing_settings, run))


if __name__ == "__main__":
    main()
