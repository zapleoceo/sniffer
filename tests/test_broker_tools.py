"""Native tool transport uses the same broker queue/accounting as text chat."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from sniffer.broker.client import BrokerClient, BrokerResult

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "catalog_search",
            "parameters": {"type": "object"},
            "strict": True,
        },
    }
]
CALLS = [
    {
        "id": "call-1",
        "type": "function",
        "function": {
            "name": "catalog_search",
            "arguments": "{}",
        },
    }
]


@pytest.fixture(autouse=True)
def instant_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    async def sleep(delay: float) -> None:
        pass

    monkeypatch.setattr("sniffer.broker.client.asyncio.sleep", sleep)


async def test_tool_only_reply_and_followup_preserve_native_wire_and_accounting() -> None:
    submitted: list[dict[str, Any]] = []
    accounted: list[BrokerResult] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            submitted.append(json.loads(request.content))
            assert request.url.params["capability"] == "chat:sales"
            return httpx.Response(202, json={"job_id": len(submitted)})
        return httpx.Response(
            200,
            json={
                "status": "done",
                "text": "" if len(submitted) == 1 else "No matches",
                "tool_calls": CALLS if len(submitted) == 1 else None,
                "finish_reason": "tool_calls" if len(submitted) == 1 else "stop",
                "refusal": None,
                "request_id": 91,
                "cost_usd": 0.001,
            },
        )

    async def sink(capability: str, result: BrokerResult) -> None:
        accounted.append(result)

    client = BrokerClient(httpx.AsyncClient(transport=httpx.MockTransport(handle)), usage=sink)
    try:
        first = await client.chat(
            [{"role": "user", "content": "Find a scooter"}],
            tools=TOOLS,
            tool_choice="auto",
            capability="chat:sales",
        )
        assert first.text == "" and first.tool_calls == CALLS
        assert first.finish_reason == "tool_calls" and not first.refusal
        history: list[dict[str, Any]] = [
            {"role": "assistant", "content": None, "tool_calls": first.tool_calls},
            {"role": "tool", "tool_call_id": "call-1", "content": '{"items":[]}'},
        ]
        second = await client.chat(history, tools=TOOLS, capability="chat:sales")
        assert second.text == "No matches" and second.tool_calls is None
    finally:
        await client.aclose()
    assert submitted[0]["tools"] == TOOLS
    assert submitted[0]["tool_choice"] == "auto"
    assert submitted[1]["messages"] == history
    assert len(accounted) == 2 and all(row.cost_usd == 0.001 for row in accounted)


@pytest.mark.parametrize(
    "options",
    [
        {"tools": TOOLS, "response_format": {"type": "json_object"}},
        {"tool_choice": "required"},
        {"tools": []},
    ],
)
async def test_invalid_tool_request_is_rejected_before_submission(options: dict[str, Any]) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    client = BrokerClient(httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        with pytest.raises(ValueError):
            await client.chat([], **options)
    finally:
        await client.aclose()
    assert requests == []


async def test_plain_chat_omits_native_fields_for_legacy_compatibility() -> None:
    payloads: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            payloads.append(json.loads(request.content))
            return httpx.Response(202, json={"job_id": 1})
        return httpx.Response(200, json={"status": "done", "text": "hello"})

    client = BrokerClient(httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        result = await client.chat([{"role": "user", "content": "hi"}])
        assert result.tool_calls is None and result.text == "hello"
    finally:
        await client.aclose()
    assert "tools" not in payloads[0] and "tool_choice" not in payloads[0]
