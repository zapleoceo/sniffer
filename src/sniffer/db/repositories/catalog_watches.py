"""Storage for personal, free MCP watches; no dependencies on old billing."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from secrets import token_urlsafe

from pydantic import BaseModel
from sqlalchemy import Select, select, text

from sniffer.db.models import Listing, catalog_watch_credentials, catalog_watch_delivered
from sniffer.db.repositories.base import Repository
from sniffer.db.repositories.public_catalog import _card, _match, _public_rows
from sniffer.domain.catalog_watches import WatchFilters
from sniffer.domain.public_catalog import PublicListing

TOKEN_PREFIX = "snw_"
MAX_WATCHES = 10


class CatalogWatch(BaseModel):
    id: int
    filters: WatchFilters
    created_at: datetime


@dataclass(frozen=True)
class DueWatch:
    id: int
    tg_chat_id: int
    filters: WatchFilters
    created_at: datetime
    after_id: int


def fingerprint(token: str) -> str:
    return sha256(token.encode("ascii")).hexdigest()


class CatalogWatchRepository(Repository):
    async def issue(self, tg_chat_id: int) -> str:
        token = TOKEN_PREFIX + token_urlsafe(32)
        await self._session.execute(
            text(
                """INSERT INTO catalog_watch_credentials (tg_chat_id, token_hash)
                VALUES (:chat, :token) ON CONFLICT (tg_chat_id)
                DO UPDATE SET token_hash = EXCLUDED.token_hash, issued_at = CURRENT_TIMESTAMP"""
            ),
            {"chat": tg_chat_id, "token": fingerprint(token)},
        )
        return token

    async def authenticate(self, token: str, *, lock: bool = False) -> int | None:
        if not token.startswith(TOKEN_PREFIX) or not token.isascii() or len(token) != 47:
            return None
        credentials = catalog_watch_credentials
        query = select(credentials.c.tg_chat_id).where(
            credentials.c.token_hash == fingerprint(token)
        )
        row = await self._session.execute(query.with_for_update() if lock else query)
        tg_chat_id = row.scalar_one_or_none()
        return int(tg_chat_id) if tg_chat_id is not None else None

    async def start(self, tg_chat_id: int, filters: WatchFilters) -> CatalogWatch:
        # The caller acquires the credential's row lock before entering this method.
        # The lock serializes watch-count and duplicate checks for concurrent calls.
        params = {"chat": tg_chat_id, "filters": filters.model_dump_json(exclude_none=True)}
        records = await self._session.execute(
            text(
                """SELECT id, filters, created_at FROM catalog_watches WHERE tg_chat_id = :chat
                AND filters = CAST(:filters AS jsonb)"""
            ),
            params,
        )
        if record := records.mappings().one_or_none():
            return CatalogWatch.model_validate(record)
        count = await self._session.execute(
            text("SELECT COUNT(*) FROM catalog_watches WHERE tg_chat_id = :chat"), params
        )
        if count.scalar_one() >= MAX_WATCHES:
            raise ValueError("You already have 10 watches. Stop an old watch first.")
        # Omit the catalog's existing contents, including freshly reprojected old posts.
        head: Select[tuple[int]] = select(Listing.id).order_by(Listing.id.desc()).limit(1)
        current = (await self._session.execute(head)).scalar_one_or_none() or 0
        result = await self._session.execute(
            text(
                """INSERT INTO catalog_watches (tg_chat_id, filters, after_id)
                VALUES (:chat, CAST(:filters AS jsonb), :head) RETURNING id, filters, created_at"""
            ),
            params | {"head": current},
        )
        return CatalogWatch.model_validate(result.mappings().one())

    async def active(self, tg_chat_id: int) -> list[CatalogWatch]:
        result = await self._session.execute(
            text(
                """SELECT id, filters, created_at FROM catalog_watches
                WHERE tg_chat_id = :chat ORDER BY id"""
            ),
            {"chat": tg_chat_id},
        )
        return [CatalogWatch.model_validate(row) for row in result.mappings()]

    async def stop(self, tg_chat_id: int, watch_id: int) -> bool:
        result = await self._session.execute(
            text(
                """DELETE FROM catalog_watches WHERE tg_chat_id = :chat AND id = :watch
                RETURNING id"""
            ),
            {"chat": tg_chat_id, "watch": watch_id},
        )
        return result.scalar_one_or_none() is not None

    async def take_due(self) -> DueWatch | None:
        result = await self._session.execute(
            text(
                """SELECT id, tg_chat_id, filters, created_at, after_id FROM catalog_watches
                WHERE next_check_at <= CURRENT_TIMESTAMP ORDER BY next_check_at, id LIMIT 1
                FOR UPDATE SKIP LOCKED"""
            )
        )
        record = result.mappings().one_or_none()
        if record is None:
            return None
        return DueWatch(
            id=record["id"],
            tg_chat_id=record["tg_chat_id"],
            filters=WatchFilters.model_validate(record["filters"]),
            created_at=record["created_at"],
            after_id=record["after_id"],
        )

    async def next_listing(
        self, watch: DueWatch, *, cities: Collection[str], models: Collection[str]
    ) -> PublicListing | None:
        delivered = catalog_watch_delivered
        seen = (
            select(delivered.c.canonical_url)
            .where(delivered.c.tg_chat_id == watch.tg_chat_id)
            .where(delivered.c.canonical_url == Listing.tg_link)
            .correlate(Listing)
            .exists()
        )
        query = _match(_public_rows(cities), watch.filters, models)
        if watch.filters.districts is not None:
            query = query.where(Listing.district.in_(watch.filters.districts))
        query = query.where(
            Listing.id > watch.after_id, Listing.posted_at >= watch.created_at, ~seen
        )
        result = await self._session.execute(query.order_by(Listing.id.asc()).limit(1))
        row = result.one_or_none()
        return _card(row, models) if row is not None else None

    async def defer(self, watch: DueWatch, *, delay: int) -> None:
        await self._session.execute(
            text(
                """UPDATE catalog_watches
                SET next_check_at = CURRENT_TIMESTAMP + make_interval(secs => :delay)
                WHERE id = :watch"""
            ),
            {"watch": watch.id, "delay": delay},
        )

    async def reserve_url(self, tg_chat_id: int, url: str) -> bool:
        result = await self._session.execute(
            text(
                """INSERT INTO catalog_watch_delivered (tg_chat_id, canonical_url)
                VALUES (:chat, :url) ON CONFLICT DO NOTHING RETURNING 1"""
            ),
            {"chat": tg_chat_id, "url": url},
        )
        return result.scalar_one_or_none() is not None
