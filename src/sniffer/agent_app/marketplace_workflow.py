"""Marketplace collection and publication for a claimed collection lease.

The lease runner in ``collector`` owns retries and replies. This module owns the
marketplace interpretation of collected originals and catalog publication.
"""

from __future__ import annotations

import structlog

from sniffer.agent_app.collector_gateway import CollectorGateway
from sniffer.agent_app.extraction import extract
from sniffer.agent_app.mcp_server import connect
from sniffer.broker.client import BrokerClient
from sniffer.broker.usage import default_usage_sink
from sniffer.db.engine import session_scope
from sniffer.db.repositories.catalog_observations import CatalogObservationRepository
from sniffer.db.repositories.collection_tasks import CollectionLease

log = structlog.get_logger(__name__)


async def process(lease: CollectionLease) -> dict[str, int]:
    """Collect assigned marketplace sources and publish source-backed listings."""
    gateway = CollectorGateway(lease)
    broker = BrokerClient(usage=default_usage_sink)
    try:
        async with connect(gateway) as session:
            # The lease contains the complete server-owned source list. Source reads
            # remain deterministic; the broker interprets retained originals only.
            for assigned_source in gateway.scope.sources:
                result = await session.call_tool("sources_collect", {"source": assigned_source})
                if result.isError or result.structuredContent is None:
                    raise ValueError("source_collection_failed")
            if set(gateway.outcomes) != set(gateway.scope.sources):
                raise ValueError("incomplete_collection")
            for index, original in enumerate(gateway.originals):
                extracted = await extract(original, broker)
                result = await session.call_tool(
                    "catalog_stage", {"index": index, "extracted": extracted}
                )
                if result.isError or result.structuredContent is None:
                    raise ValueError("stage_failed")
        async with session_scope() as db:
            repo = CatalogObservationRepository(db)
            for successful_source in gateway.outcomes:
                await repo.record_coverage(lease.id, lease.token, successful_source, "success")
            await db.commit()
        return {"collected": len(gateway.originals), "published": gateway.published}
    except Exception as exc:
        log.exception("collector.task_failed", task_id=lease.id, kind=type(exc).__name__)
        # Partial publication stays valid; incomplete extraction is never fresh.
        try:
            async with session_scope() as db:
                repo = CatalogObservationRepository(db)
                for source in gateway.scope.sources:
                    await repo.record_coverage(lease.id, lease.token, source, "error")
                await db.commit()
        except Exception:
            log.warning("collector.coverage_write_failed", task_id=lease.id)
        raise
    finally:
        await broker.aclose()
