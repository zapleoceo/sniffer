"""Native tool-call turns through AIbroker only, with no provider SDK or repair."""

from __future__ import annotations

from typing import Any, Protocol

from sniffer.agents.contracts import AgentError, ToolCall, Turn
from sniffer.broker.client import BrokerError


class BrokerToolResult(Protocol):
    @property
    def text(self) -> str: ...

    @property
    def tool_calls(self) -> list[dict[str, Any]] | None: ...

    @property
    def finish_reason(self) -> str | None: ...

    @property
    def refusal(self) -> bool: ...


class ToolBroker(Protocol):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        tool_choice: str,
        capability: str,
        max_tokens: int,
        temperature: float,
    ) -> BrokerToolResult: ...


class BrokerModel:
    def __init__(
        self,
        broker: ToolBroker,
        *,
        capability: str = "chat:sales",
        max_tokens: int = 2048,
    ) -> None:
        if type(max_tokens) is not int or max_tokens <= 0:
            raise ValueError("max_tokens must be a positive integer")
        self._broker, self._capability, self._max_tokens = broker, capability, max_tokens

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn:
        try:
            result = await self._broker.chat(
                messages,
                tools=tools,
                tool_choice="auto",
                capability=self._capability,
                max_tokens=self._max_tokens,
                temperature=0.1,
            )
        except BrokerError:
            raise AgentError("broker_failed") from None
        if type(result.refusal) is not bool or result.refusal:
            raise AgentError("refusal")
        if not isinstance(result.text, str) or not isinstance(result.finish_reason, str):
            raise AgentError("invalid_broker_turn")
        raw_calls = result.tool_calls
        if raw_calls is not None and not isinstance(raw_calls, list):
            raise AgentError("invalid_tool_calls")
        calls = tuple(_call(raw) for raw in (raw_calls or []))
        return Turn(result.text, calls, result.finish_reason)


def _call(raw: dict[str, Any]) -> ToolCall:
    if not isinstance(raw, dict) or raw.get("type") != "function":
        raise AgentError("invalid_tool_call")
    function = raw.get("function")
    if not isinstance(function, dict):
        raise AgentError("invalid_tool_call")
    call_id, name, arguments = raw.get("id"), function.get("name"), function.get("arguments")
    if not isinstance(call_id, str) or not isinstance(name, str) or not isinstance(arguments, str):
        raise AgentError("invalid_tool_call")
    return ToolCall(call_id, name, arguments)
