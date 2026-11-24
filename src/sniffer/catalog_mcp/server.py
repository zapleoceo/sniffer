"""Stateless Streamable HTTP facade; no tools from the internal agent server."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from sniffer.catalog_mcp.address import MCP_PATH, PERSONAL_PATH, PUBLIC_HOST
from sniffer.catalog_mcp.watch_tools import MODELS, register_watches
from sniffer.db.engine import session_scope
from sniffer.db.repositories.public_catalog import PublicCatalogRepository
from sniffer.domain.districts import PLACES
from sniffer.domain.passport import Category
from sniffer.domain.public_catalog import (
    CatalogGuide,
    Cursor,
    Filters,
    Location,
    Page,
    PageSize,
    PublicListing,
)
from sniffer.search.market_terms import ALL_CITY_NAMES

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)


def make_server(*, personal: bool = False) -> FastMCP:
    catalog = FastMCP(
        "SnifferBot Personal Catalog" if personal else "SnifferBot Public Catalog",
        instructions=(
            "Browse active public listings and open their original links. "
            "Use catalog_guide for filter values. For another page, pass next_before_id "
            "as before_id with the same filters. Unknown facts are null. "
            "This catalog does not guarantee that a source listing is still available."
        ),
        streamable_http_path=PERSONAL_PATH if personal else MCP_PATH,
        stateless_http=True,
        json_response=True,
        max_request_body_size=16384,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[PUBLIC_HOST, f"{PUBLIC_HOST}:443", "127.0.0.1:8005"],
            allowed_origins=[f"https://{PUBLIC_HOST}"],
        ),
    )
    db_slots = asyncio.Semaphore(3)

    @asynccontextmanager
    async def repository() -> AsyncIterator[PublicCatalogRepository]:
        # Keep catalog traffic out of most dashboard connections, set hard limits on both
        # pool wait and execution, and never leak DB detail to a caller.
        try:
            async with asyncio.timeout(0.25):
                await db_slots.acquire()
        except TimeoutError:
            raise RuntimeError("Catalog is busy; try again shortly.") from None
        try:
            async with asyncio.timeout(4), session_scope() as session:
                yield PublicCatalogRepository(session, cities=ALL_CITY_NAMES, models=MODELS)
        except (ValueError, asyncio.CancelledError):
            raise
        except Exception:
            raise RuntimeError("Catalog temporarily unavailable.") from None
        finally:
            db_slots.release()

    @catalog.tool(annotations=READ_ONLY)
    async def search_listings(
        filters: Filters | None = None, before_id: Cursor | None = None, limit: PageSize = 20
    ) -> Page:
        """Filter public listings by location, category, deal, price, or known features."""
        async with repository() as repo:
            return await repo.page(filters or Filters(), before_id=before_id, limit=limit)

    @catalog.tool(annotations=READ_ONLY)
    async def browse_listings(before_id: Cursor | None = None, limit: PageSize = 20) -> Page:
        """Browse the full public catalog without applying search filters."""
        async with repository() as repo:
            return await repo.page(Filters(), before_id=before_id, limit=limit)

    @catalog.tool(annotations=READ_ONLY)
    async def get_listing(listing_id: Cursor) -> PublicListing:
        """Look up one active public listing, including its original source URL."""
        async with repository() as repo:
            result = await repo.detail(listing_id)
        if result is None:
            raise ValueError("Public listing not found.")
        return result

    @catalog.tool(annotations=READ_ONLY)
    def catalog_guide() -> CatalogGuide:
        """List understood categories, areas, sources and deal types for search filters."""
        return CatalogGuide(
            cities={slug: names.get("vi", slug) for slug, names in ALL_CITY_NAMES.items()},
            districts=[Location(slug=p.slug, name=p.name, city=p.city) for p in PLACES],
            categories=list(Category),
            models=sorted(MODELS),
            deal_types=["sell", "rent_out", "wanted"],
            sources=["telegram", "chotot"],
        )

    if personal:
        register_watches(catalog)
    return catalog
