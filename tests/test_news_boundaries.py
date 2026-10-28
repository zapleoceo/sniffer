"""News is a separate, disabled scenario until an editorial pipeline exists."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from sniffer.agent_app import marketplace_workflow, scenario_router
from sniffer.agent_app.contracts import CollectionScope
from sniffer.db.repositories.collection_tasks import CollectionLease
from sniffer.domain.news import MediaRights, NewsDraft
from sniffer.domain.scenarios import ScenarioUnavailable

NOW = datetime.now(UTC)


def lease(scenario: object | None = None) -> CollectionLease:
    scope: dict[str, object] = {
        "city": "nha_trang",
        "category": "apartment",
        "deal_type": "rent_out",
    }
    if scenario is not None:
        scope["scenario"] = scenario
    return CollectionLease(1, "token", scope, 1, NOW)


async def test_legacy_scope_routes_to_marketplace(monkeypatch: pytest.MonkeyPatch) -> None:
    marketplace = AsyncMock(return_value={"collected": 1, "published": 1})
    monkeypatch.setattr(marketplace_workflow, "process", marketplace)

    assert await scenario_router.process(lease()) == {"collected": 1, "published": 1}
    marketplace.assert_awaited_once()


@pytest.mark.parametrize("scenario", ["news", "unknown", 42])
async def test_unavailable_scenarios_do_not_reach_marketplace(
    monkeypatch: pytest.MonkeyPatch, scenario: object
) -> None:
    marketplace = AsyncMock()
    monkeypatch.setattr(marketplace_workflow, "process", marketplace)

    with pytest.raises(ScenarioUnavailable):
        await scenario_router.process(lease(scenario))

    marketplace.assert_not_awaited()


def test_marketplace_scope_accepts_legacy_and_rejects_news() -> None:
    scope = {"city": "nha_trang", "category": "apartment", "deal_type": "rent_out"}
    parsed = CollectionScope.model_validate(scope)
    assert parsed.scenario == "marketplace"
    assert "scenario" not in parsed.model_dump(mode="json")
    assert CollectionScope.model_validate({**scope, "scenario": "marketplace"}).model_dump(
        mode="json"
    ) == parsed.model_dump(mode="json")
    with pytest.raises(ValidationError):
        CollectionScope.model_validate({**scope, "scenario": "news"})


def test_news_rights_are_unknown_until_evidence_exists() -> None:
    draft = NewsDraft(claims=(), independent_source_ids=frozenset())
    assert draft.media_rights is MediaRights.UNKNOWN
