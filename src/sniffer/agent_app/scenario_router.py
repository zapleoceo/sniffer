"""Resolve the scenario before any source or broker call."""

from __future__ import annotations

from collections.abc import Mapping

from sniffer.agent_app import marketplace_workflow, news_workflow
from sniffer.db.repositories.collection_tasks import CollectionLease
from sniffer.domain.scenarios import Scenario, from_scope


def require_available(scope: Mapping[str, object]) -> None:
    """Reject disabled and unknown scopes, including reply-only recovery."""
    if from_scope(scope) is Scenario.NEWS:
        news_workflow.require_available()


async def process(lease: CollectionLease) -> dict[str, int]:
    if from_scope(lease.scope) is Scenario.NEWS:
        return await news_workflow.process(lease)
    return await marketplace_workflow.process(lease)
