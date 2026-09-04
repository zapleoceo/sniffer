"""Select the rollout path using server-owned dialogue identity, never model text."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

import structlog

from sniffer.bot.conversation import Finder, Found, find_live
from sniffer.bot.store import Dialogue
from sniffer.config import Settings, get_settings
from sniffer.sources.base import RawItem

log = structlog.get_logger(__name__)


class Answer(Protocol):
    @property
    def items(self) -> list[RawItem]: ...

    @property
    def status(self) -> str | None: ...


class CatalogSearch(Protocol):
    async def __call__(
        self, user_id: int, request_id: int, version: int, *, allow_collection: bool
    ) -> Answer: ...


async def _catalog(
    user_id: int, request_id: int, version: int, *, allow_collection: bool
) -> Answer:
    # Legacy mode must not initialize the new runtime or require its server.
    from sniffer.agent_app.main import search_request

    return await search_request(user_id, request_id, version, allow_collection=allow_collection)


class CatalogFinder:
    def __init__(
        self,
        *,
        legacy: Finder = find_live,
        catalog: CatalogSearch = _catalog,
        settings: Callable[[], Settings] = get_settings,
    ) -> None:
        self._legacy = legacy
        self._catalog = catalog
        self._settings = settings

    async def __call__(self, dialogue: Dialogue) -> Found:
        current = dialogue.passport
        if current is None or current.user_id != dialogue.user_id:
            raise ValueError("invalid_request_scope")
        settings = self._settings()
        mode = settings.catalog_mode
        enabled = mode == "catalog" or (
            mode == "pilot" and dialogue.user_id in settings.catalog_pilot_user_ids
        )
        if enabled:
            return await self._search(dialogue, allow_collection=True)
        found = await self._legacy(current.passport)
        if mode == "shadow":
            # Sequential, bounded, no detached task: cancellation cannot leave
            # an unowned background operation. Shadow never enqueues collection.
            try:
                async with asyncio.timeout(5):
                    shadow = await self._search(dialogue, allow_collection=False)
                log.info(
                    "bot.catalog_shadow", legacy_count=len(found.items), count=len(shadow.items)
                )
            except Exception:
                log.warning("bot.catalog_shadow_failed")
        return found

    async def _search(self, dialogue: Dialogue, *, allow_collection: bool) -> Found:
        current = dialogue.passport
        if current is None:
            raise ValueError("missing_request")
        answer = await self._catalog(
            dialogue.user_id, current.root, current.version, allow_collection=allow_collection
        )
        return Found(
            items=answer.items,
            sources=tuple(sorted({item.source for item in answer.items})),
            status=answer.status,
        )
