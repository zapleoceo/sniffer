"""Поиск по уже собранным и нормализованным карточкам PostgreSQL."""

from __future__ import annotations

from typing import Any

import structlog

from sniffer.domain.records import Listing
from sniffer.sources.base import RawItem, Source, register
from sniffer.sources.listing_catalog import ListingCatalog, new_catalog

log = structlog.get_logger(__name__)

SOURCE_NAME = "archive"
LIMIT = 100


@register
class ArchiveSource(Source):
    name = SOURCE_NAME

    def __init__(self, catalog: ListingCatalog | None = None) -> None:
        super().__init__()
        self._catalog = catalog or new_catalog()

    async def search(self, query: str, params: dict[str, Any]) -> list[RawItem]:
        del query  # структурная выборка; текстовое соответствие решает общий ranker
        try:
            return [_item(row) for row in await self._catalog.search(params, limit=LIMIT)]
        except Exception as exc:
            self.degraded = True
            log.warning("archive.search_failed", error=f"{type(exc).__name__}: {exc}")
            return []


def _item(row: Listing) -> RawItem:
    return RawItem(
        source=row.source,
        external_id=row.external_id or str(row.id),
        url=row.tg_link,
        title=row.title,
        text=row.summary,
        price_raw=str(row.price_amount or ""),
        price_vnd=int(row.price_amount) if row.price_amount is not None else None,
        posted_at=row.posted_at,
        raw={"listing_id": row.id, "category": row.category, "attributes": row.attributes},
    )
