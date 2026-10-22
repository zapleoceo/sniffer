"""Уборка карточек старше срока актуальности. Выключена по умолчанию.

Команда `python -m sniffer.worker stale-cleanup` (`worker/stale_cleanup.py`):
сухой прогон по умолчанию, удаление — только с явным подтверждением. Расписания
у уборки нет, и воркер её сам не запускает.

Что удаляется: `listings.posted_at < cutoff` — и только это. Не удаляется:

* карточка с `catalog_observation_id` — это проекция общего каталога, и её
  `posted_at` мог прийти из `fetched_at` (`catalog_listing_projection.py`:
  `observed.posted_at or observed.fetched_at`), то есть быть датой нашего
  захода, а не датой объявления. В схеме одно от другого не отличить, поэтому
  исключается вся группа;
* карточка, у которой есть показы (`offer_views`): FK без CASCADE, история квоты
  клиента не должна исчезать молча.

Что уходит вместе с карточкой по CASCADE: `listing_media`, `notifications`.
`catalog_observations` получает SET NULL (но проекции мы и так не трогаем);
`raw_messages` не затрагиваются: сырьё живёт по своему сроку (`retention`).

Пачка: `SELECT ... FOR UPDATE SKIP LOCKED` → `DELETE` по id в одной транзакции с
`lock_timeout`/`statement_timeout`. Блокировка строк держит и FK-гонку: показ,
вставленный параллельно, ждёт конца транзакции и после неё упадёт на FK.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, delete, exists, func, select, text
from sqlalchemy.orm import InstrumentedAttribute

from sniffer.db import models
from sniffer.db.repositories.base import Repository

LOCK_TIMEOUT = "2s"
STATEMENT_TIMEOUT = "30s"


@dataclass(frozen=True, slots=True)
class StaleBatch:
    """Итог одной пачки. В сухом прогоне `deleted == 0`, остальное — «было бы»."""

    selected: int
    deleted: int
    media: int
    notifications: int
    last_id: int


@dataclass(frozen=True, slots=True)
class StaleOverview:
    """Сколько карточек старше границы и почему часть из них неприкосновенна."""

    stale_total: int
    protected_catalog: int
    protected_offer_views: int
    eligible: int


def _shown(listing_id: InstrumentedAttribute[int]) -> ColumnElement[bool]:
    return exists().where(models.OfferView.listing_id == listing_id)


class StaleListingCleanup(Repository):
    async def overview(self, cutoff: datetime) -> StaleOverview:
        """Разрез по причинам защиты. Только чтение."""
        old = models.Listing.posted_at < cutoff
        shown = _shown(models.Listing.id)
        catalog = models.Listing.catalog_observation_id.is_not(None)
        row = (
            await self._session.execute(
                select(
                    func.count(),
                    func.count().filter(catalog),
                    func.count().filter(~catalog, shown),
                    func.count().filter(~catalog, ~shown),
                ).where(old)
            )
        ).one()
        return StaleOverview(*(int(value) for value in row))

    async def purge_batch(
        self, cutoff: datetime, batch_size: int, *, after_id: int = 0, dry_run: bool = True
    ) -> StaleBatch:
        """Одна пачка по ключу `id > after_id`. Коммит — за вызывающим.

        Курсор нужен сухому прогону: он ничего не удаляет, и без него каждая
        пачка выбирала бы одни и те же строки.
        """
        if batch_size < 1:
            raise ValueError("batch_size_must_be_positive")
        await self._session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        await self._session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
        listing = models.Listing
        pick = (
            select(listing.id)
            .where(
                listing.posted_at < cutoff,
                listing.posted_at.is_not(None),
                listing.catalog_observation_id.is_(None),
                ~_shown(listing.id),
                listing.id > after_id,
            )
            .order_by(listing.id)
            .limit(batch_size)
        )
        if not dry_run:
            pick = pick.with_for_update(skip_locked=True)
        ids = list(await self._session.scalars(pick))
        if not ids:
            return StaleBatch(0, 0, 0, 0, after_id)
        media = await self._session.scalar(
            select(func.count())
            .select_from(models.ListingMedia)
            .where(models.ListingMedia.listing_id.in_(ids))
        )
        notifications = await self._session.scalar(
            select(func.count())
            .select_from(models.Notification)
            .where(models.Notification.listing_id.in_(ids))
        )
        deleted = 0
        if not dry_run:
            # Повторное условие на показы — страховка поверх блокировки строк.
            result = await self._session.execute(
                delete(listing)
                .where(listing.id.in_(ids), ~_shown(listing.id))
                .returning(listing.id)
            )
            deleted = len(result.scalars().all())
        return StaleBatch(len(ids), deleted, int(media or 0), int(notifications or 0), ids[-1])
