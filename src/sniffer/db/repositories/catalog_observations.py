"""Fenced staging and monotonic publication. The caller owns the transaction."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from sniffer.db.models.catalog import coverage, observations, publications
from sniffer.db.repositories.base import Repository
from sniffer.db.repositories.collection_tasks import CollectionTaskRepository
from sniffer.domain.catalog import CatalogObservation


class CatalogRejected(ValueError):
    """Safe reason codes, never raw advertisement text."""


class CatalogObservationRepository(Repository):
    async def stage(self, task_id: int, token: str, observation: CatalogObservation) -> int:
        # Revalidate even a model_construct/model_copy instance supplied by trusted Python code.
        observation = CatalogObservation.model_validate_json(observation.model_dump_json())
        lease = await CollectionTaskRepository(self._session).require_lease(task_id, token)
        self._source_scope(lease.scope, observation.source)
        now = await self._session.scalar(select(func.clock_timestamp()))
        if now is None or observation.fetched_at > now + timedelta(minutes=5):
            raise CatalogRejected("future_observation")
        payload = observation.model_dump(mode="json")
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        statement = (
            insert(observations)
            .values(
                task_id=task_id,
                source=observation.source,
                external_id=observation.external_id,
                content_hash=digest,
                fetched_at=observation.fetched_at,
                payload=payload,
            )
            .on_conflict_do_nothing(
                index_elements=["task_id", "source", "external_id", "content_hash"]
            )
            .returning(observations.c.id)
        )
        identifier = await self._session.scalar(statement)
        if identifier is None:
            identifier = await self._session.scalar(
                select(observations.c.id).where(
                    observations.c.task_id == task_id,
                    observations.c.source == observation.source,
                    observations.c.external_id == observation.external_id,
                    observations.c.content_hash == digest,
                )
            )
        assert identifier is not None
        return int(identifier)

    async def publish(self, task_id: int, token: str, observation_id: int) -> bool:
        lease = await CollectionTaskRepository(self._session).require_lease(task_id, token)
        row = (
            (
                await self._session.execute(
                    select(observations).where(
                        observations.c.id == observation_id,
                        observations.c.task_id == task_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise CatalogRejected("observation_not_in_task")
        observation = CatalogObservation.model_validate_json(json.dumps(row["payload"]))
        self._source_scope(lease.scope, observation.source)
        facts = observation.facts
        if not observation.publishable:
            return False
        if (
            facts.city != lease.scope.get("city")
            or facts.category != lease.scope.get("category")
            or facts.deal_type != lease.scope.get("deal_type")
        ):
            raise CatalogRejected("facts_outside_task_scope")
        values = {
            "source": observation.source,
            "external_id": observation.external_id,
            "observation_id": observation_id,
            "city": facts.city,
            "category": facts.category,
            "deal_type": facts.deal_type,
            "price_vnd": facts.price_vnd,
            "active": facts.active,
            "fetched_at": observation.fetched_at,
            "verified_at": func.clock_timestamp(),
        }
        statement = insert(publications).values(**values)
        publish_statement = statement.on_conflict_do_update(
            index_elements=["source", "external_id"],
            set_={
                key: statement.excluded[key]
                for key in values
                if key not in {"source", "external_id"}
            },
            # Late retries cannot resurrect an old price/deletion or overwrite equal-time conflict.
            where=statement.excluded.fetched_at > publications.c.fetched_at,
        ).returning(publications.c.observation_id)
        return await self._session.scalar(publish_statement) is not None

    async def search(
        self,
        *,
        city: str,
        category: str,
        deal_type: str | None = None,
        max_price_vnd: int | None = None,
        limit: int = 20,
        fresh_seconds: int = 86400,
    ) -> list[dict[str, Any]]:
        if (
            not city
            or not category
            or type(limit) is not int
            or not 1 <= limit <= 50
            or type(fresh_seconds) is not int
            or not 1 <= fresh_seconds <= 604800
        ):
            raise CatalogRejected("invalid_search_bounds")
        if max_price_vnd is not None and (
            type(max_price_vnd) is not int or not 0 <= max_price_vnd <= 10**15
        ):
            raise CatalogRejected("invalid_price")
        statement = (
            select(observations.c.payload, publications.c.verified_at)
            .join(
                publications,
                publications.c.observation_id == observations.c.id,
            )
            .where(
                publications.c.city == city,
                publications.c.category == category,
                publications.c.active.is_(True),
                publications.c.fetched_at <= func.clock_timestamp(),
                publications.c.fetched_at
                >= func.clock_timestamp() - timedelta(seconds=fresh_seconds),
            )
        )
        if deal_type is not None:
            statement = statement.where(publications.c.deal_type == deal_type)
        if max_price_vnd is not None:
            # Unknown prices are not quietly advertised as inside a hard budget.
            statement = statement.where(publications.c.price_vnd <= max_price_vnd)
        rows = (
            await self._session.execute(
                statement.order_by(
                    publications.c.fetched_at.desc(),
                    publications.c.source,
                    publications.c.external_id,
                ).limit(limit)
            )
        ).mappings()
        return [
            {"observation": row["payload"], "verified_at": row["verified_at"].isoformat()}
            for row in rows
        ]

    async def record_coverage(
        self,
        task_id: int,
        token: str,
        source: str,
        outcome: Literal["success", "error", "unsupported"],
    ) -> None:
        if outcome not in {"success", "error", "unsupported"}:
            raise CatalogRejected("invalid_coverage_outcome")
        lease = await CollectionTaskRepository(self._session).require_lease(task_id, token)
        self._source_scope(lease.scope, source)
        statement = insert(coverage).values(
            scope_key=self._scope_key(lease.scope),
            source=source,
            task_id=task_id,
            checked_at=func.clock_timestamp(),
            outcome=outcome,
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=["scope_key", "source"],
                set_={key: statement.excluded[key] for key in ("task_id", "checked_at", "outcome")},
            )
        )

    async def coverage(self, scope: dict[str, Any], *, fresh_seconds: int = 3600) -> dict[str, Any]:
        if type(fresh_seconds) is not int or not 1 <= fresh_seconds <= 86400:
            raise CatalogRejected("invalid_freshness")
        sources = scope.get("sources")
        if (
            not isinstance(sources, list)
            or not sources
            or len(sources) > 20
            or any(not isinstance(source, str) or not source for source in sources)
        ):
            raise CatalogRejected("invalid_sources")
        rows = (
            await self._session.execute(
                select(coverage).where(
                    coverage.c.scope_key == self._scope_key(scope),
                    coverage.c.source.in_(sources),
                )
            )
        ).mappings()
        now = await self._session.scalar(select(func.clock_timestamp()))
        states: dict[str, str] = {source: "not_collected" for source in sources}
        for row in rows:
            states[row["source"]] = (
                "stale"
                if (now - row["checked_at"]).total_seconds() > fresh_seconds
                else "fresh"
                if row["outcome"] == "success"
                else row["outcome"]
            )
        return {"sources": states}

    @staticmethod
    def _source_scope(scope: dict[str, Any], source: str) -> None:
        sources = scope.get("sources")
        if not isinstance(sources, list) or source not in sources:
            raise CatalogRejected("source_outside_task_scope")

    @staticmethod
    def _scope_key(scope: dict[str, Any]) -> str:
        # Budget/query/attribute coverage must not be borrowed from an unrelated search.
        canonical = {key: value for key, value in scope.items() if key != "sources"}
        return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
