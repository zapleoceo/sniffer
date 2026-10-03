"""Журнал показов на словарях: квота без Postgres, но по тем же правилам.

Подделка одна на тесты диалога, тесты квоты и симулятор — по образцу
`stubs.MemoryStore`. Что она обязана делать так же, как `QuotaRepository`,
закреплено контрактными тестами: они идут на обеих (`tests/test_quota_ledger.py`).
Подделка, которая «упростила» правило, мерила бы не бота, а себя.

Чего подделка не воспроизводит: блокировок и ограничений базы (CHECK границ,
составные ключи, уникальность). Гонки и барьеры проверяет только живой Postgres
(`tests/test_quota_db.py`); здесь всё выполняется в одном потоке событийного
цикла без `await` внутри операции, то есть атомарно по построению.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sniffer.domain.quota import (
    Channel,
    Claim,
    Reserved,
    Ticket,
    Usage,
    decide,
    numbered_period,
    unique,
)
from sniffer.domain.quota_period import Period

SWEPT = (Channel.SEARCH, Channel.DEFERRED)


@dataclass(slots=True)
class View:
    """Строка журнала: одна карточка в одном периоде."""

    user_id: int
    period_id: int
    listing_id: int
    channel: Channel
    shown_at: datetime
    last_shown_at: datetime
    times_shown: int = 1
    delivered_at: datetime | None = None
    passport_root: int | None = None
    request_id: int | None = None


@dataclass(slots=True)
class Counters:
    shown: int = 0
    withheld: int = 0


class MemoryLedger:
    def __init__(self) -> None:
        self.anchors: dict[int, datetime] = {}
        self.period_ids: dict[tuple[int, int], int] = {}
        self.bounds: dict[int, Period] = {}
        self.views: dict[tuple[int, int], View] = {}
        self.offered: dict[int, datetime] = {}
        self.counters: dict[int, Counters] = {}

    def rows(self, user_id: int) -> list[View]:
        return [view for view in self.views.values() if view.user_id == user_id]

    async def reserve(self, claim: Claim) -> Reserved:
        ids = unique(claim.listing_ids)
        if not ids:
            raise ValueError("нечего резервировать: список карточек пуст")
        anchor = self.anchors.setdefault(claim.user_id, claim.now)
        number, period = numbered_period(anchor, claim.now)
        period_id = self.period_ids.setdefault((claim.user_id, number), len(self.period_ids) + 1)
        self.bounds[period_id] = period
        seen = {i for i in ids if (period_id, i) in self.views}
        used = sum(
            1
            for (pid, _), view in self.views.items()
            if pid == period_id and view.channel is not Channel.MONITOR
        )
        decision = decide(ids, seen=seen, used=used, limit=claim.limit)
        for listing_id in decision.granted:
            self.views[(period_id, listing_id)] = View(
                user_id=claim.user_id,
                period_id=period_id,
                listing_id=listing_id,
                channel=claim.channel,
                shown_at=claim.now,
                last_shown_at=claim.now,
                passport_root=claim.passport_root,
                request_id=claim.request_id,
            )
        for listing_id in decision.repeated:
            view = self.views[(period_id, listing_id)]
            view.times_shown += 1
            view.last_shown_at = claim.now
        if decision.withheld and claim.request_id is not None:
            self.counters.setdefault(claim.request_id, Counters()).withheld += len(
                decision.withheld
            )
        return Reserved(period_id=period_id, period=period, decision=decision)

    async def confirm(self, ticket: Ticket, at: datetime) -> None:
        for listing_id in ticket.shown:
            view = self.views.get((ticket.period_id, listing_id))
            if view is not None and view.delivered_at is None:
                view.delivered_at = at
        if ticket.request_id is not None and ticket.shown:
            self.counters.setdefault(ticket.request_id, Counters()).shown += len(ticket.shown)

    async def release(self, ticket: Ticket) -> None:
        for listing_id in ticket.granted:
            view = self.views.get((ticket.period_id, listing_id))
            if view is not None and view.delivered_at is None:
                del self.views[(ticket.period_id, listing_id)]

    async def usage(self, user_id: int, now: datetime) -> Usage:
        anchor = self.anchors.get(user_id)
        if anchor is None:
            return Usage(used=0, period_end=None)
        number, period = numbered_period(anchor, now)
        period_id = self.period_ids.get((user_id, number))
        used = sum(
            1
            for (pid, _), view in self.views.items()
            if pid == period_id and view.channel is not Channel.MONITOR
        )
        return Usage(used=used, period_end=period.end)

    async def claim_offer(self, user_id: int, now: datetime, cooldown: timedelta) -> bool:
        last = self.offered.get(user_id)
        if last is not None and last > now - cooldown:
            return False
        self.offered[user_id] = now
        return True

    async def sweep(self, older_than: datetime, limit: int) -> int:
        stale = sorted(
            (
                (view.shown_at, key)
                for key, view in self.views.items()
                if view.delivered_at is None
                and view.shown_at < older_than
                and view.channel in SWEPT
            ),
        )[:limit]
        for _, key in stale:
            del self.views[key]
        return len(stale)
