"""Гашение карточек по возрасту — «крон» в процессе воркера.

Возраст — самый грубый признак неактуальности, но единственный, который есть у
каждой карточки: слова «продано» продавец часто не пишет вовсе, а просто
перестаёт отвечать. Живое объявление продавец переопубликует, и дедуп
(`worker/archive.py`) поднимет старую карточку на свежую дату, — поэтому
гашение устаревшего не теряет продающегося, а лишь убирает забытое.

Раз в час, пачками, как уборка сырья (`worker/retention.py`): первое гашение
накопленного (2433 карточки на 18.09.2026) не держит длинную транзакцию.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import structlog

from sniffer.db.engine import session_scope
from sniffer.db.repositories.listings import ListingRepository
from sniffer.domain.listing_state import LISTING_MAX_AGE_DAYS

log = structlog.get_logger(__name__)

EXPIRE_EVERY_S = 60 * 60
BATCH = 1000

Now = Callable[[], datetime]
Monotonic = Callable[[], float]
Expire = Callable[[datetime, int], Awaitable[int]]


async def expire_once(older_than: datetime, limit: int) -> int:
    async with session_scope() as session:
        expired = await ListingRepository(session).expire(older_than=older_than, limit=limit)
        await session.commit()
        return expired


class Expiry:
    """Расписание гашения. Первый заход — сразу после старта процесса."""

    def __init__(
        self,
        *,
        days: int = LISTING_MAX_AGE_DAYS,
        every_s: float = EXPIRE_EVERY_S,
        batch: int = BATCH,
        expire: Expire = expire_once,
        now: Now = lambda: datetime.now(UTC),
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        self._days, self._every_s, self._batch = days, every_s, batch
        self._expire, self._now, self._monotonic = expire, now, monotonic
        self._due_at = monotonic()

    async def tick(self) -> int:
        if self._monotonic() < self._due_at:
            return 0
        older_than = self._now() - timedelta(days=self._days)
        expired = await self._expire(older_than, self._batch)
        if expired:
            log.info("listings.expired", expired=expired, older_than=older_than.isoformat())
        if expired >= self._batch:
            # Пачка полная — устаревшего осталось ещё; срок не сдвигаем.
            return expired
        self._due_at = self._monotonic() + self._every_s
        return expired
