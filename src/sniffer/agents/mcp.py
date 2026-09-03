"""MCP ClientSession protocol adapter, not a server or a transport launcher.

The caller supplies an already authenticated session to an authorized read-only
server. Discovery and model-supplied annotations never grant new permissions.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Protocol

from sniffer.agents.contracts import AgentError


class McpResult(Protocol):
    @property
    def isError(self) -> bool | None: ...

    @property
    def structuredContent(self) -> dict[str, Any] | None: ...


class McpSession(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpResult: ...


class McpReadTools:
    def __init__(self, session: McpSession, *, allowed_read_tools: frozenset[str]) -> None:
        self._session = session
        self._allowed = allowed_read_tools

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self._allowed:
            raise AgentError("unknown_tool")
        try:
            result = await self._session.call_tool(name, deepcopy(arguments))
            is_error, structured = result.isError, result.structuredContent
        except Exception:
            raise AgentError("mcp_call_failed") from None
        if is_error is not None and (type(is_error) is not bool or is_error):
            raise AgentError("mcp_tool_failed")
        if not isinstance(structured, dict):
            raise AgentError("mcp_missing_structured_content")
        return deepcopy(structured)
