"""Explicit application scenarios; legacy collection scopes belong to marketplace."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class Scenario(StrEnum):
    MARKETPLACE = "marketplace"
    NEWS = "news"


class ScenarioUnavailable(ValueError):
    """A scope names an unknown or unavailable scenario."""


def from_scope(scope: Mapping[str, object]) -> Scenario:
    """Resolve a server-owned scope without broadening legacy tasks."""
    raw = scope.get("scenario", Scenario.MARKETPLACE.value)
    if not isinstance(raw, str):
        raise ScenarioUnavailable("unknown_scenario")
    try:
        return Scenario(raw)
    except ValueError as exc:
        raise ScenarioUnavailable("unknown_scenario") from exc
