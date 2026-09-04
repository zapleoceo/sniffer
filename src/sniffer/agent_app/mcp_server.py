"""Private MCP server: authenticated by possession of a bound in-process session.

No socket, bearer token, environment/SQL/shell tool or remote discovery exists.
The composition root supplies a gateway bound to one principal before initialize.
The MCP SDK handles real initialize/list_tools/call_tool messages over memory streams.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from mcp import ClientSession, types
from mcp.server import Server
from mcp.shared.memory import create_connected_server_and_client_session

from sniffer.agent_app.contracts import Gateway
from sniffer.broker.output import parse_object


def build_server(gateway: Gateway) -> Server[Any]:
    server: Server[Any] = Server("sniffer-scoped-tools")
    specs = {spec.name: spec for spec in gateway.specs}

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=s.name,
                description=s.description,
                inputSchema=s.input_schema,
                outputSchema=s.output_schema,
            )
            for s in specs.values()
        ]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            if name not in specs:
                raise ValueError("unknown_tool")
            payload = json.dumps(arguments, allow_nan=False)
            if len(payload) > 32000:
                raise ValueError("arguments_too_large")
            parsed = parse_object(payload, specs[name].input_schema)
            result = await gateway.call(name, parsed)
            return types.CallToolResult(content=[], structuredContent=result)
        except Exception:
            # Source text/SQL errors never become a prompt or a client diagnostic.
            return types.CallToolResult(
                isError=True, content=[types.TextContent(type="text", text="tool_rejected")]
            )

    return server


@asynccontextmanager
async def connect(gateway: Gateway) -> AsyncIterator[ClientSession]:
    async with create_connected_server_and_client_session(
        build_server(gateway),
        read_timeout_seconds=timedelta(seconds=30),
    ) as session:
        yield session
