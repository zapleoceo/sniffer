"""Trusted session authority is never read from model tool arguments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from sniffer.agents.contracts import ToolSpec


class SearchCriteria(BaseModel):
    """Server-derived search hints; never free-form model arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    key: str = Field(pattern=r"^[0-9a-f]{64}$")
    brand: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=80)
    transmission: Literal["automatic", "manual", "semi"] | None = None
    engine_cc: int | None = Field(default=None, ge=1, le=5000)
    engine_cc_dir: Literal["min", "max"] | None = None
    rooms: int | None = Field(default=None, ge=0, le=20)
    furnished: bool | None = None
    districts: tuple[str, ...] = Field(default=(), max_length=20)
    must_have: tuple[str, ...] = Field(default=(), max_length=20)
    deal_breakers: tuple[str, ...] = Field(default=(), max_length=20)
    budget_min: int | None = Field(default=None, ge=0, le=10**15)
    budget_max: int | None = Field(default=None, ge=0, le=10**15)
    budget_currency: Literal["VND", "USD", "EUR", "RUB"] | None = None


class CollectionScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    city: Literal["nha_trang", "da_nang"]
    category: Literal["motorbike", "bicycle", "car", "apartment", "room", "house", "other"]
    deal_type: Literal["sell", "rent_out", "wanted"]
    sources: tuple[Literal["chotot", "archive"], ...] = ("chotot", "archive")
    # Backward-compatible only for tasks already queued before criteria-aware
    # collection. New MainGateway scopes always provide a request fingerprint.
    criteria: SearchCriteria = Field(default_factory=lambda: SearchCriteria(key="0" * 64))
    max_items: int = Field(default=12, ge=1, le=20)
    max_calls: int = Field(default=2, ge=1, le=2)


@dataclass(frozen=True, slots=True)
class MainIdentity:
    user_id: int
    request_id: int
    version: int
    allow_collection: bool = True


class Gateway(Protocol):
    @property
    def specs(self) -> tuple[ToolSpec, ...]: ...

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


def tool(name: str, description: str, properties: dict[str, Any] | None = None) -> ToolSpec:
    props = properties or {}
    return ToolSpec(
        name,
        description,
        {
            "type": "object",
            "properties": props,
            "required": list(props),
            "additionalProperties": False,
        },
        {"type": "object"},
    )
