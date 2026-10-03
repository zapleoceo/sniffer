"""`LedgerSlots`: число слотов — живые подписки из журнала платежей; проводка воркера."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.worker import __main__ as worker_main
from sniffer.worker import slot_ledger
from sniffer.worker.slot_ledger import LedgerSlots

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


class Scope:
    async def __aenter__(self) -> AsyncSession:
        return cast(AsyncSession, "session")

    async def __aexit__(self, *_: object) -> None:
        return None


async def test_the_count_is_the_number_of_live_subscriptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[Any, int, datetime]] = []

    class Billing:
        def __init__(self, session: Any) -> None:
            self.session = session

        async def live_subscriptions(self, user_id: int, now: datetime) -> int:
            asked.append((self.session, user_id, now))
            return 2

    monkeypatch.setattr(slot_ledger, "BillingRepository", Billing)

    assert await LedgerSlots(sessions=lambda: Scope()).count(7, NOW) == 2
    assert asked == [("session", 7, NOW)]


def test_the_worker_builds_the_monitor_with_the_real_slots_not_unlimited() -> None:
    assert isinstance(worker_main.build_monitor()._slots, LedgerSlots)
