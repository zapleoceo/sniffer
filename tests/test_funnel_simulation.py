"""Independent checks for the mandatory category/city/budget funnel.

These assertions deliberately spell out the intended request instead of deriving
it from ``parse_query``.  A parser can consistently misread a request while the
simulation still appears internally self-consistent.
"""

from __future__ import annotations

import pytest

from sniffer.simulation.harness import run_scenario
from sniffer.simulation.scenarios import SCENARIOS
from sniffer.simulation.script import Scenario


def _scenario(key: str) -> Scenario:
    return next(scenario for scenario in SCENARIOS if scenario.key == key)


@pytest.mark.asyncio
async def test_plain_automatic_scooter_fills_buy_city_and_budget() -> None:
    metrics = await run_scenario(_scenario("plain_scooter_automatic_funnel"))

    assert metrics.passport_fields["intent"] == "buy"
    assert metrics.passport_fields["category"] == "motorbike"
    assert metrics.passport_fields["attributes.transmission"] == "automatic"
    assert metrics.passport_fields["city"] == "da_nang"
    assert metrics.passport_fields["budget.max"] == 500.0
    assert metrics.passport_fields["budget.currency"] == "USD"
    assert metrics.asked_fields == ("city", "budget.max")
    assert not metrics.repeated_questions


@pytest.mark.asyncio
async def test_yamaha_refinement_preserves_da_nang_when_budget_changes() -> None:
    metrics = await run_scenario(_scenario("yamaha_da_nang_refinement"))

    assert metrics.passport_fields["intent"] == "buy"
    assert metrics.passport_fields["category"] == "motorbike"
    assert metrics.passport_fields["attributes.brand"] == "yamaha"
    assert metrics.passport_fields["city"] == "da_nang"
    assert metrics.passport_fields["budget.max"] == 700.0
    assert metrics.passport_fields["budget.currency"] == "USD"
    assert metrics.asked_fields == ("city", "budget.max")
    assert not metrics.repeated_questions
