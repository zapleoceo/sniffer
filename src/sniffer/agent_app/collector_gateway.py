"""Task-bound source reads and fenced MCP staging. No arbitrary SQL/URL tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.agent_app.contracts import CollectionScope, tool
from sniffer.agent_app.extraction import Original, observation
from sniffer.agents.contracts import ToolSpec
from sniffer.db.engine import session_scope
from sniffer.db.repositories.catalog_observations import CatalogObservationRepository
from sniffer.db.repositories.collection_sources import CollectionSourceRepository
from sniffer.db.repositories.collection_tasks import CollectionLease, CollectionTaskRepository
from sniffer.search.currency import usd_vnd_rate
from sniffer.sources.chotot import ChototSource

Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]
Fetch = Callable[[str, CollectionScope, int], Awaitable[list[Original]]]


async def fetch_source(source: str, scope: CollectionScope, limit: int) -> list[Original]:
    if source == "archive":
        archive_scope = scope
        if scope.criteria.budget_currency == "USD":
            budget = await _budget_vnd(scope)
            assert budget is not None
            archive_scope = scope.model_copy(
                update={
                    "criteria": scope.criteria.model_copy(
                        update={
                            "budget_min": budget["min"],
                            "budget_max": budget["max"],
                            "budget_currency": "VND",
                        }
                    )
                }
            )
        async with session_scope() as session:
            rows = await CollectionSourceRepository(session).archive(
                archive_scope.model_dump(mode="json"), limit=limit
            )
        return [
            Original(
                "archive",
                f"{row['chat_tg_id']}:{row['msg_id']}",
                f"https://t.me/{row['username']}/{row['msg_id']}",
                row["text"][:200],
                row["text"][:30000],
                row["ingested_at"],
                row["posted_at"],
            )
            for row in rows
        ]
    if source != "chotot":
        raise ValueError("source_not_allowed")
    if scope.category != "motorbike" or scope.deal_type != "sell":
        raise ValueError("source_not_supported_for_scope")
    adapter = ChototSource()
    try:
        criteria = scope.criteria
        query = " ".join(
            value.replace("_", " ") for value in (criteria.brand, criteria.model) if value
        )
        attributes = {
            key: value
            for key, value in {
                "brand": criteria.brand,
                "model": criteria.model,
                "transmission": criteria.transmission,
                "engine_cc": criteria.engine_cc,
                "engine_cc_dir": criteria.engine_cc_dir,
                "rooms": criteria.rooms,
                "furnished": criteria.furnished,
            }.items()
            if value is not None
        }
        budget = await _budget_vnd(scope)
        rows_live = await adapter.search(
            query,
            {
                "city": scope.city,
                "category": scope.category,
                "attributes": attributes,
                "budget": budget,
                "limit": limit,
            },
        )
        if adapter.degraded:
            raise RuntimeError("source_unavailable")
        stamp = datetime.now(UTC)
        return [
            Original(
                "chotot",
                row.external_id,
                row.url,
                row.title[:500],
                # Source-owned raw JSON preserves location/category evidence omitted
                # by the display-oriented title/text mapper. No passport fields here.
                _raw_text(row.title, row.text, row.raw),
                stamp,
                row.posted_at,
            )
            for row in rows_live[:limit]
        ]
    finally:
        await adapter.aclose()


async def _budget_vnd(scope: CollectionScope) -> dict[str, int | str] | None:
    criteria = scope.criteria
    if criteria.budget_min is None and criteria.budget_max is None:
        return None
    if criteria.budget_currency == "VND":
        return {
            "min": criteria.budget_min or 0,
            "max": criteria.budget_max or 10**15,
            "currency": "VND",
        }
    if criteria.budget_currency == "USD":
        rate = await usd_vnd_rate()
        if rate is None:
            raise RuntimeError("currency_rate_unavailable")
        return {
            "min": int((criteria.budget_min or 0) * rate),
            "max": int((criteria.budget_max or 10**15) * rate),
            "currency": "VND",
        }
    raise ValueError("budget_currency_not_supported_by_source")


def _raw_text(title: str, text: str, raw: dict[str, Any]) -> str:
    import json

    return (title + "\n" + text + "\n" + json.dumps(raw, ensure_ascii=False, allow_nan=False))[
        :30000
    ]


class CollectorGateway:
    def __init__(
        self,
        lease: CollectionLease,
        *,
        sessions: Sessions = session_scope,
        fetch: Fetch = fetch_source,
    ) -> None:
        self.lease = lease
        self.scope = CollectionScope.model_validate(lease.scope)
        self._sessions, self._fetch = sessions, fetch
        self.originals: list[Original] = []
        self.outcomes: dict[str, str] = {}
        self._staged: set[int] = set()
        self.published = 0
        self.specs = (
            tool(
                "sources_collect",
                "Collect bounded source records in the assigned scope",
                {
                    "source": {"type": "string", "enum": list(self.scope.sources)},
                },
            ),
            tool(
                "catalog_stage",
                "Validate and publish source-indexed extraction",
                {
                    "index": {"type": "integer", "minimum": 0, "maximum": 11},
                    "extracted": {"type": "object"},
                },
            ),
        )

    @property
    def read_specs(self) -> tuple[ToolSpec, ...]:
        return self.specs[:1]

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "sources_collect":
            return await self._collect(arguments["source"])
        if name == "catalog_stage":
            return await self._stage(arguments["index"], arguments["extracted"])
        raise ValueError("unknown_tool")

    async def _collect(self, source: str) -> dict[str, Any]:
        if source not in self.scope.sources or source in self.outcomes:
            raise ValueError("source_outside_scope_or_repeated")
        if len(self.outcomes) >= self.scope.max_calls:
            raise ValueError("source_call_limit")
        async with self._sessions() as session:
            await CollectionTaskRepository(session).require_lease(self.lease.id, self.lease.token)
            await session.commit()
        self.outcomes[source] = "error"
        total = min(12, self.scope.max_items)
        limit = min(total - len(self.originals), max(1, total // len(self.scope.sources)))
        if limit < 1:
            raise ValueError("item_limit")
        originals = await self._fetch(source, self.scope, limit)
        if len(originals) > limit or any(item.source != source for item in originals):
            raise ValueError("invalid_source_result")
        self.originals.extend(originals)
        self.outcomes[source] = "success"
        return {"count": len(originals), "source": source}

    async def _stage(self, index: int, extracted: dict[str, Any]) -> dict[str, Any]:
        if type(index) is not int or not 0 <= index < len(self.originals):
            raise ValueError("unknown_original")
        if index in self._staged:
            raise ValueError("repeated_original")
        observed = observation(self.originals[index], extracted)
        async with self._sessions() as session:
            repo = CatalogObservationRepository(session)
            identifier = await repo.stage(self.lease.id, self.lease.token, observed)
            # Facts from another city/category are retained in staging; not published.
            matches = (
                observed.facts.city == self.scope.city
                and observed.facts.category == self.scope.category
            )
            published = matches and await repo.publish(self.lease.id, self.lease.token, identifier)
            await session.commit()
        self._staged.add(index)
        self.published += int(published)
        return {"observation_id": identifier, "published": published}
