"""Заготовки тестов квоты: один набор сценариев на подделку и на Postgres.

Контракт журнала проверяется на обеих реализациях `Ledger`: `MemoryLedger`
работает всегда, `SqlLedger` — только с `TEST_DATABASE_URL`. Подделка, которую не
сверяют с настоящей, мерит саму себя; сверка идёт теми же сценариями, а не
парой похожих тестов, которые разошлись бы на первой правке.

Набор («комплект») прячет разницу между реализациями: завести клиента и
карточки, посмотреть, что записано в журнале. Всё остальное сценарии делают
через `QuotaService`, как бот.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from sniffer.bot.quota import Account, Entitlements, Ledger, QuotaService
from sniffer.bot.quota_ledger import SqlLedger
from sniffer.db import models
from sniffer.simulation.ledger import Counters, MemoryLedger


@dataclass(frozen=True, slots=True)
class Row:
    """Строка журнала так, как её видит сценарий."""

    listing_id: int
    channel: str
    times_shown: int
    delivered: bool


class Kit(Protocol):
    ledger: Ledger

    async def new_user(self) -> int: ...

    async def listings(self, count: int) -> list[int]: ...

    async def new_request(self, user_id: int) -> int: ...

    async def ref_of(self, listing_id: int) -> tuple[str, str]: ...

    async def rows(self, user_id: int) -> list[Row]: ...

    async def anchor(self, user_id: int) -> datetime | None: ...

    async def counters(self, request_id: int) -> tuple[int, int]: ...

    async def period_count(self, user_id: int) -> int: ...


class Clock:
    """Часы под управлением теста: подставной «сейчас» вместо настоящего."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def tick(self, delta: timedelta) -> datetime:
        self.now += delta
        return self.now


class MemoryKit:
    def __init__(self) -> None:
        self.memory = MemoryLedger()
        self.ledger: Ledger = self.memory
        self._users = itertools.count(1)
        self._listings = itertools.count(1000)
        self._requests = itertools.count(1)

    async def new_user(self) -> int:
        return next(self._users)

    async def listings(self, count: int) -> list[int]:
        made = [next(self._listings) for _ in range(count)]
        for listing_id in made:
            self.memory.known[("memory", f"m{listing_id}")] = listing_id
        return made

    async def ref_of(self, listing_id: int) -> tuple[str, str]:
        return ("memory", f"m{listing_id}")

    async def new_request(self, user_id: int) -> int:
        return next(self._requests)

    async def rows(self, user_id: int) -> list[Row]:
        views = sorted(self.memory.rows(user_id), key=lambda view: view.listing_id)
        return [
            Row(
                view.listing_id, view.channel.value, view.times_shown, view.delivered_at is not None
            )
            for view in views
        ]

    async def anchor(self, user_id: int) -> datetime | None:
        return self.memory.anchors.get(user_id)

    async def counters(self, request_id: int) -> tuple[int, int]:
        found = self.memory.counters.get(request_id, Counters())
        return found.shown, found.withheld

    async def period_count(self, user_id: int) -> int:
        return sum(1 for owner, _ in self.memory.period_ids if owner == user_id)


class SqlKit:
    """Те же вопросы к настоящим таблицам; клиенты и карточки — настоящие строки (внешние ключи)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.ledger: Ledger = SqlLedger(self.sessions)
        self._seq = itertools.count(1)

    async def new_user(self) -> int:
        async with self.sessions() as session, session.begin():
            user = models.User(tg_user_id=7_000_000 + next(self._seq))
            session.add(user)
            await session.flush()
            return user.id

    async def listings(self, count: int) -> list[int]:
        async with self.sessions() as session, session.begin():
            made = [
                models.Listing(
                    source="quota_test",
                    external_id=f"quota-{next(self._seq)}",
                    deal_type="sell",
                    category="motorbike",
                    city="nha_trang",
                    title="Honda Vision",
                    summary="Honda Vision, 25 млн",
                    tg_link="https://t.me/quota/1",
                    posted_at=datetime.now(UTC),
                )
                for _ in range(count)
            ]
            session.add_all(made)
            await session.flush()
            return [listing.id for listing in made]

    async def ref_of(self, listing_id: int) -> tuple[str, str]:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(models.Listing.source, models.Listing.external_id).where(
                        models.Listing.id == listing_id
                    )
                )
            ).one()
            return str(row[0]), str(row[1])

    async def new_request(self, user_id: int) -> int:
        async with self.sessions() as session, session.begin():
            request = models.ClientRequest(user_id=user_id, raw_query="квота")
            session.add(request)
            await session.flush()
            return request.id

    async def rows(self, user_id: int) -> list[Row]:
        async with self.sessions() as session:
            views = await session.scalars(
                select(models.OfferView)
                .where(models.OfferView.user_id == user_id)
                .order_by(models.OfferView.listing_id)
            )
            return [
                Row(v.listing_id, v.channel, v.times_shown, v.delivered_at is not None)
                for v in views
            ]

    async def anchor(self, user_id: int) -> datetime | None:
        async with self.sessions() as session:
            return await session.scalar(
                select(models.User.quota_anchor_at).where(models.User.id == user_id)
            )

    async def counters(self, request_id: int) -> tuple[int, int]:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(
                        models.ClientRequest.shown_count, models.ClientRequest.withheld_count
                    ).where(models.ClientRequest.id == request_id)
                )
            ).one()
            return int(row[0]), int(row[1])

    async def period_count(self, user_id: int) -> int:
        async with self.sessions() as session:
            counted = await session.scalar(
                select(func.count())
                .select_from(models.QuotaPeriod)
                .where(models.QuotaPeriod.user_id == user_id)
            )
            return int(counted or 0)


T0 = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)
OWNER_TG = 169510539


class Slots:
    """Право тарифа под управлением теста: сколько слотов подписки у всех сейчас."""

    def __init__(self, count: int = 0) -> None:
        self.count = count

    async def slots(self, account: Account, now: datetime) -> int:
        return self.count


def account(user_id: int) -> Account:
    return Account(user_id=user_id, tg_user_id=1000 + user_id)


def service(
    kit: Kit,
    clock: Clock | None = None,
    *,
    entitlements: Entitlements | None = None,
    owner_tg_id: int | None = None,
) -> QuotaService:
    return QuotaService(
        kit.ledger,
        entitlements=entitlements,
        clock=clock or Clock(T0),
        owner_tg_id=owner_tg_id,
    )


async def started(kit: Kit) -> tuple[int, Account]:
    user = await kit.new_user()
    return user, account(user)
