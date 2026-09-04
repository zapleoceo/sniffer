"""Small injected interfaces; agents know neither SQL nor provider SDKs."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]

    def wire(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": deepcopy(self.input_schema),
                "strict": True,
            },
        }


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class Turn:
    text: str
    calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    refused: bool = False


class Model(Protocol):
    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Turn: ...


class ReadTools(Protocol):
    """Only trusted, server-authorized READ tools belong to the stage-0 runtime."""

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class AgentError(RuntimeError):
    """Safe error code; does not expose arguments or source contents."""
