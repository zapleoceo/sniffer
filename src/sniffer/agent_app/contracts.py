"""Trusted session authority is never read from model tool arguments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from sniffer.agents.contracts import ToolSpec


class CollectionScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    city: Literal["nha_trang", "da_nang"]
    category: Literal["motorbike", "bicycle", "car", "apartment", "room", "house", "other"]
    deal_type: Literal["sell", "rent_out", "wanted"]
    sources: tuple[Literal["chotot", "archive"], ...] = ("chotot", "archive")
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
