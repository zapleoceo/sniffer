"""Catalogue-only answer assembly: model prose cannot become a listing card."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import structlog

from sniffer.agent_app.contracts import MainIdentity
from sniffer.agent_app.main_gateway import MainGateway
from sniffer.agent_app.mcp_server import connect
from sniffer.agents.broker_model import BrokerModel
from sniffer.agents.mcp import McpReadTools
from sniffer.agents.runtime import ReadAgent
from sniffer.broker.client import BrokerClient
from sniffer.broker.usage import default_usage_sink
from sniffer.db.repositories.agent_requests import AgentRequestRepository
from sniffer.domain.catalog import CatalogObservation
from sniffer.search.currency import usd_vnd_rate
from sniffer.search.relevance import rank_items
from sniffer.sources.base import RawItem

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CatalogAnswer:
    items: list[RawItem]
    status: str | None = None


async def search_request(
    user_id: int,
    request_id: int,
    version: int,
    *,
    allow_collection: bool = True,
) -> CatalogAnswer:
    gateway = MainGateway(MainIdentity(user_id, request_id, version, allow_collection))
    broker = BrokerClient(usage=default_usage_sink)
    try:
        async with asyncio.timeout(25), connect(gateway) as session:
            tools = frozenset(spec.name for spec in gateway.specs)
            runtime = ReadAgent(
                BrokerModel(broker),
                McpReadTools(session, allowed_read_tools=tools),
                gateway.specs,
                allowed_read_tools=tools,
                deadline_s=18,
            )
            try:
                await runtime.run(
                    "Read catalog_search and catalog_coverage for the bound request. "
                    "Do not invent or modify search conditions. Summarize availability."
                )
            except Exception as exc:
                # Broker failure never enables external sources or a different model.
                log.warning("catalog.agent_fallback", kind=type(exc).__name__)
            # The deterministic read also covers a model which chose no tool at all.
            result = await session.call_tool("catalog_search", {})
            if result.isError:
                raise PermissionError("catalog_unavailable")
            async with gateway.sessions() as db:
                request = await AgentRequestRepository(db).owned(user_id, request_id, version)
            items = [_item(row["observation"]) for row in gateway.rows]
            rate = await usd_vnd_rate() if request.passport.budget.currency == "USD" else None
            items = rank_items(request.passport, items, usd_vnd=rate)
            status = await gateway.queue_if_needed()
            if request.passport.budget.currency == "USD" and rate is None:
                items = []
                status = (
                    "Не удалось проверить курс для бюджета в долларах. "
                    "Попробуйте позже или укажите бюджет в донгах."
                )
            return CatalogAnswer(items, status)
    finally:
        await broker.aclose()


def _item(data: dict[str, object]) -> RawItem:
    # Validated publication payload, not generated card text.
    observation = CatalogObservation.model_validate_json(json.dumps(data))
    return RawItem(
        source=observation.source,
        external_id=observation.external_id,
        url=observation.url,
        title=observation.title,
        text=observation.raw_text,
        price_vnd=observation.facts.price_vnd,
        price_raw=next((e.quote for e in observation.evidence if e.field == "price_vnd"), ""),
        posted_at=observation.posted_at,
    )
