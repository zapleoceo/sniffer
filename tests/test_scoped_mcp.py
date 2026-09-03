"""Actual MCP initialize/tools transport, with repository-independent authority tests."""

from __future__ import annotations

from typing import Any

import pytest

from sniffer.agent_app.contracts import CollectionScope, tool
from sniffer.agent_app.mcp_server import connect


class BoundGateway:
    specs = (tool("catalog_search", "Only current authenticated user's request"),)

    def __init__(self) -> None:
        self.calls = 0

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {"items": [], "request_id": 12}


async def test_real_mcp_initialize_list_and_structured_result() -> None:
    gateway = BoundGateway()
    async with connect(gateway) as session:
        listed = await session.list_tools()
        assert [item.name for item in listed.tools] == ["catalog_search"]
        result = await session.call_tool("catalog_search", {})
        assert not result.isError and result.structuredContent == {"items": [], "request_id": 12}
    assert gateway.calls == 1


@pytest.mark.parametrize(
    "name,args",
    [
        ("catalog_search", {"user_id": 99}),
        ("catalog_search", {"city": "foreign"}),
        ("execute_sql", {"sql": "DROP TABLE users"}),
        ("sources_collect", {"url": "http://127.0.0.1"}),
    ],
)
async def test_model_cannot_change_principal_filters_or_capabilities(
    name: str,
    args: dict[str, Any],
) -> None:
    gateway = BoundGateway()
    async with connect(gateway) as session:
        result = await session.call_tool(name, args)
        assert result.isError
    assert gateway.calls == 0


async def test_mcp_gateway_errors_do_not_expose_database_or_source_content() -> None:
    class Broken(BoundGateway):
        async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("private database credential")

    async with connect(Broken()) as session:
        result = await session.call_tool("catalog_search", {})
        assert result.isError and "private" not in str(result)


@pytest.mark.parametrize(
    "change",
    [
        {"sources": ["telegram_groups"]},
        {"max_calls": 99},
        {"max_items": 999},
        {"sql": "anything"},
        {"city": "https://example.com"},
    ],
)
def test_scope_does_not_allow_live_telegram_sql_urls_or_unbounded_work(
    change: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        CollectionScope.model_validate(
            {
                "city": "nha_trang",
                "category": "motorbike",
                "deal_type": "sell",
                **change,
            }
        )
