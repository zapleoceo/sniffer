"""Disabled news workflow: no collection, broker calls, or publication."""

from __future__ import annotations

from typing import Never

from sniffer.db.repositories.collection_tasks import CollectionLease
from sniffer.domain.scenarios import ScenarioUnavailable


def require_available() -> Never:
    raise ScenarioUnavailable("news_disabled")


async def process(_lease: CollectionLease) -> dict[str, int]:
    require_available()
