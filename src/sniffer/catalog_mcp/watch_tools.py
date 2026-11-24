"""Personal MCP watch tools scoped to the proven Telegram destination."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request

from sniffer.db.engine import session_scope
from sniffer.db.repositories.catalog_watches import CatalogWatch, CatalogWatchRepository
from sniffer.db.repositories.public_catalog import validate_filters
from sniffer.domain.catalog_watches import WatchFilters
from sniffer.domain.public_catalog import Cursor
from sniffer.search.market_terms import ALL_CITY_NAMES
from sniffer.search.motorbike_models import MOTORBIKE_MODELS

MODELS = frozenset(model.slug for model in MOTORBIKE_MODELS)
_Ctx = Context[Any, Any, Request]
_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
_START = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
_STOP = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)


def bearer_token(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token if scheme.lower() == "bearer" else ""


@asynccontextmanager
async def _scope(
    ctx: _Ctx, *, mutation: bool = False
) -> AsyncIterator[tuple[CatalogWatchRepository, int]]:
    request = ctx.request_context.request
    if not isinstance(request, Request):
        raise ValueError("A personal Telegram MCP key is required.")
    try:
        async with (
            asyncio.timeout(4),
            session_scope() as session,
        ):
            repo = CatalogWatchRepository(session)
            chat = await repo.authenticate(bearer_token(request), lock=mutation)
            if chat is None:
                raise ValueError("Invalid personal MCP key. Get a new one via Telegram /mcp.")
            yield repo, chat
            if mutation:
                await session.commit()
    except (ValueError, asyncio.CancelledError):
        raise
    except Exception:
        raise RuntimeError("Watch management temporarily unavailable.") from None


def register_watches(server: FastMCP) -> None:
    @server.tool(annotations=_START)
    async def create_watch(ctx: _Ctx, filters: WatchFilters) -> CatalogWatch:
        """Start free Telegram updates for new matching public listings; no backlog."""
        if not filters.city or not filters.category or not filters.deal_type:
            raise ValueError("Specify city, category and deal_type before subscribing.")
        if filters.city not in ALL_CITY_NAMES:
            raise ValueError("Unknown city; use catalog_guide.")
        validate_filters(filters, MODELS)
        async with _scope(ctx, mutation=True) as (repo, chat):
            return await repo.start(chat, filters)

    @server.tool(annotations=_READ)
    async def list_watches(ctx: _Ctx) -> list[CatalogWatch]:
        """List only the watches attached to your own Telegram account."""
        async with _scope(ctx) as (repo, chat):
            return await repo.active(chat)

    @server.tool(annotations=_STOP)
    async def stop_watch(ctx: _Ctx, watch_id: Cursor) -> bool:
        """Stop one of your own Telegram watches, identified by list_watches."""
        async with _scope(ctx, mutation=True) as (repo, chat):
            return await repo.stop(chat, watch_id)
