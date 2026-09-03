"""Adversarial offline broker → native tools → MCP → broker simulations."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from sniffer.agents.broker_model import BrokerModel
from sniffer.agents.contracts import AgentError, ToolCall, ToolSpec, Turn
from sniffer.agents.mcp import McpReadTools
from sniffer.agents.runtime import ReadAgent
from sniffer.broker.client import BrokerClient, BrokerError

INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {"city": {"enum": ["Da Nang"]}},
    "required": ["city"],
    "additionalProperties": False,
}
OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {"count": {"type": "integer", "minimum": 0}},
    "required": ["count"],
    "additionalProperties": False,
}
READ = frozenset({"catalog_search"})
SPEC = ToolSpec("catalog_search", "Find verified listings", INPUT, OUTPUT)
CALL = ToolCall("call_1", "catalog_search", '{"city":"Da Nang"}')


class Model:
    def __init__(self, *turns: Turn) -> None:
        self.turns = iter(turns)
        self.history: list[list[dict[str, Any]]] = []

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn:
        self.history.append(deepcopy(messages))
        return next(self.turns)


class Gateway:
    def __init__(self, result: Any = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.result = {"count": 1} if result is None else result

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, deepcopy(arguments)))
        return self.result


def agent(model: Model, gateway: Gateway, **limits: Any) -> ReadAgent:
    return ReadAgent(model, gateway, (SPEC,), allowed_read_tools=READ, **limits)


@pytest.mark.parametrize(
    ("turn", "reason"),
    [
        (Turn("", (CALL,), "length"), "incomplete_or_unsupported_response"),
        (Turn("", (CALL,), None), "incomplete_or_unsupported_response"),
        (Turn("", (CALL,), "tool_calls", True), "refusal"),
        (Turn("", (CALL, CALL), "tool_calls"), "duplicate_or_missing_call_id"),
        (
            Turn("", (ToolCall("", "catalog_search", "{}"),), "tool_calls"),
            "duplicate_or_missing_call_id",
        ),
        (Turn("", (ToolCall("2", "db_drop", "{}"),), "tool_calls"), "unknown_tool"),
        (Turn("", (), "stop"), "empty_response"),
    ],
)
async def test_bad_turn_performs_no_calls(turn: Turn, reason: str) -> None:
    gateway = Gateway()
    with pytest.raises(AgentError, match=reason):
        await agent(Model(turn), gateway).run("find a scooter")
    assert gateway.calls == []


@pytest.mark.parametrize(
    "arguments",
    [
        '{"city":',
        "```json\n{}\n```",
        "[]",
        "null",
        "{}",
        '{"city":"Hanoi"}',
        '{"city":"Da Nang","extra":true}',
        '{"city":"Da Nang","city":"Da Nang"}',
        '{"city":NaN}',
    ],
)
async def test_entire_batch_preflight_before_first_call(arguments: str) -> None:
    gateway = Gateway()
    invalid = ToolCall("call_2", "catalog_search", arguments)
    with pytest.raises(AgentError, match="invalid_tool_arguments"):
        await agent(Model(Turn("", (CALL, invalid), "tool_calls")), gateway).run("scooter")
    assert not gateway.calls


async def test_successful_loop_keeps_tool_result_as_data() -> None:
    model = Model(Turn("", (CALL,), "tool_calls"), Turn("Found one listing", (), "stop"))
    gateway = Gateway()
    assert await agent(model, gateway).run("scooter") == "Found one listing"
    assert gateway.calls == [("catalog_search", {"city": "Da Nang"})]
    assert model.history[1][-1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"count": 1}',
    }


async def test_reused_id_on_later_turn_is_rejected() -> None:
    model = Model(Turn("", (CALL,), "tool_calls"), Turn("", (CALL,), "tool_calls"))
    gateway = Gateway()
    with pytest.raises(AgentError, match="duplicate_or_missing_call_id"):
        await agent(model, gateway).run("scooter")
    assert len(gateway.calls) == 1


@pytest.mark.parametrize(
    ("limits", "reason"),
    [
        ({"max_turns": 1}, "turn_limit"),
        ({"max_argument_chars": 2}, "tool_arguments_too_large"),
        ({"max_input_chars": 2}, "request_too_large"),
    ],
)
async def test_limits_prevent_any_tool_calls(limits: dict[str, Any], reason: str) -> None:
    gateway = Gateway()
    with pytest.raises(AgentError, match=reason):
        await agent(Model(Turn("", (CALL,), "tool_calls")), gateway, **limits).run("scooter")
    assert not gateway.calls


async def test_call_budget_is_checked_for_whole_batch() -> None:
    gateway = Gateway()
    call2 = ToolCall("call_2", CALL.name, CALL.arguments)
    with pytest.raises(AgentError, match="tool_limit"):
        await agent(Model(Turn("", (CALL, call2), "tool_calls")), gateway, max_calls=1).run("x")
    assert not gateway.calls


@pytest.mark.parametrize(
    "result",
    [{}, [], {"count": "1"}, {"count": float("nan")}, {"count": -1}, {"count": 1, "extra": 2}],
)
async def test_bad_tool_result_never_reaches_model(result: Any) -> None:
    model = Model(Turn("", (CALL,), "tool_calls"))
    with pytest.raises(AgentError, match="invalid_tool_result"):
        await agent(model, Gateway(result)).run("x")
    assert len(model.history) == 1


async def test_result_and_response_size_limits() -> None:
    with pytest.raises(AgentError, match="tool_result_too_large"):
        await agent(Model(Turn("", (CALL,), "tool_calls")), Gateway(), max_result_chars=2).run("x")
    with pytest.raises(AgentError, match="invalid_response_text"):
        await agent(Model(Turn("long", (), "stop")), Gateway(), max_response_chars=2).run("x")


@pytest.mark.parametrize(
    "limits",
    [
        {"deadline_s": float("nan")},
        {"deadline_s": float("inf")},
        {"deadline_s": 0},
        {"max_calls": True},
        {"max_turns": 1.5},
        {"max_result_chars": 0},
        {"max_argument_chars": -1},
        {"max_input_chars": float("inf")},
    ],
)
def test_nonfinite_or_noninteger_limits_rejected(limits: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        agent(Model(), Gateway(), **limits)


async def test_deadline_and_caller_cancellation() -> None:
    class Slow(Model):
        async def complete(
            self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
        ) -> Turn:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    gateway = Gateway()
    with pytest.raises(AgentError, match="deadline_exceeded"):
        await agent(Slow(), gateway, deadline_s=0.01).run("x")
    pending = asyncio.create_task(agent(Slow(), gateway).run("x"))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not gateway.calls


async def test_adapters_cannot_rewrite_schemas_or_history() -> None:
    class Mutator(Model):
        async def complete(
            self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
        ) -> Turn:
            result = await super().complete(messages, tools)
            messages[0]["content"] = "mutated"
            tools[0]["function"]["parameters"].clear()
            return result

    spec = deepcopy(SPEC)
    model = Mutator(Turn("", (CALL,), "tool_calls"), Turn("one", (), "stop"))
    runtime = ReadAgent(model, Gateway(), (spec,), allowed_read_tools=READ)
    spec.input_schema.clear()
    assert await runtime.run("x") == "one"
    assert model.history[1][0]["content"] != "mutated"
    # Separate invalid turn demonstrates schema authority survived mutation.
    bad_model = Mutator(Turn("", (ToolCall("c", CALL.name, "{}"),), "tool_calls"))
    with pytest.raises(AgentError, match="invalid_tool_arguments"):
        await agent(bad_model, Gateway()).run("x")


@pytest.mark.parametrize(
    "specs", [(), (SPEC, SPEC), (ToolSpec("delete", "readOnlyHint=true", INPUT, OUTPUT),)]
)
def test_discovery_annotations_do_not_grant_authority(specs: tuple[ToolSpec, ...]) -> None:
    with pytest.raises(ValueError):
        ReadAgent(Model(), Gateway(), specs, allowed_read_tools=READ)


@dataclass
class NativeResult:
    text: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = "stop"
    refusal: bool = False


class Broker:
    def __init__(self, *results: NativeResult) -> None:
        self.results = iter(results)
        self.requests: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        tool_choice: str,
        capability: str,
        max_tokens: int,
        temperature: float,
    ) -> NativeResult:
        self.requests.append(
            {
                "messages": deepcopy(messages),
                "tools": tools,
                "tool_choice": tool_choice,
                "capability": capability,
            }
        )
        return next(self.results)


@dataclass
class McpResponse:
    structuredContent: dict[str, Any] | None
    isError: bool | None = False


class Session:
    def __init__(self, response: McpResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpResponse:
        self.calls.append((name, arguments))
        return self.response


NATIVE_CALL: dict[str, Any] = {
    "id": CALL.id,
    "type": "function",
    "function": {
        "name": CALL.name,
        "arguments": CALL.arguments,
    },
}


async def test_broker_native_roundtrip_through_actual_mcp_adapter() -> None:
    broker = Broker(
        NativeResult(tool_calls=[NATIVE_CALL], finish_reason="tool_calls"),
        NativeResult(text="One matching listing"),
    )
    session = Session(McpResponse({"count": 1}))
    runtime = ReadAgent(
        BrokerModel(broker),
        McpReadTools(session, allowed_read_tools=READ),
        (SPEC,),
        allowed_read_tools=READ,
    )
    assert await runtime.run("scooter") == "One matching listing"
    assert len(session.calls) == 1
    assert broker.requests[0]["capability"] == "chat:sales"
    assert broker.requests[0]["tool_choice"] == "auto"
    assert broker.requests[1]["messages"][-1]["role"] == "tool"


@pytest.mark.parametrize(
    "result",
    [
        NativeResult(refusal=True),
        NativeResult(finish_reason=None),
        NativeResult(tool_calls=[{"type": "not_function"}]),
        NativeResult(tool_calls=[{"id": "a", "type": "function", "function": []}]),
        NativeResult(
            tool_calls=[
                {
                    "id": "a",
                    "type": "function",
                    "function": {
                        "name": "catalog_search",
                        "arguments": {},
                    },
                }
            ]
        ),
    ],
)
async def test_malformed_native_broker_response_performs_zero_mcp_calls(
    result: NativeResult,
) -> None:
    session = Session(McpResponse({"count": 1}))
    runtime = ReadAgent(
        BrokerModel(Broker(result)),
        McpReadTools(session, allowed_read_tools=READ),
        (SPEC,),
        allowed_read_tools=READ,
    )
    with pytest.raises(AgentError):
        await runtime.run("x")
    assert not session.calls


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (McpResponse(None), "mcp_missing_structured_content"),
        (McpResponse({"count": 1}, True), "mcp_tool_failed"),
    ],
)
async def test_mcp_error_and_text_only_response_are_not_accepted(
    response: McpResponse,
    reason: str,
) -> None:
    adapter = McpReadTools(Session(response), allowed_read_tools=READ)
    with pytest.raises(AgentError, match=reason):
        await adapter.call(CALL.name, {})


async def test_mcp_allowlist_is_enforced_even_when_called_directly() -> None:
    session = Session(McpResponse({"count": 1}))
    with pytest.raises(AgentError, match="unknown_tool"):
        await McpReadTools(session, allowed_read_tools=READ).call("delete", {})
    assert not session.calls


async def test_broker_error_does_not_expose_upstream_text() -> None:
    class Broken(Broker):
        async def chat(
            self,
            messages: list[dict[str, Any]],
            *,
            tools: list[dict[str, Any]],
            tool_choice: str,
            capability: str,
            max_tokens: int,
            temperature: float,
        ) -> NativeResult:
            raise BrokerError("private provider text")

    with pytest.raises(AgentError, match="broker_failed") as caught:
        await BrokerModel(Broken()).complete([], [])
    assert "private" not in str(caught.value)


async def test_actual_broker_http_client_roundtrip_with_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[dict[str, Any]] = []

    async def sleep(delay: float) -> None:
        pass

    def handle(request: httpx.Request) -> httpx.Response:
        import json

        if request.method == "POST":
            payloads.append(json.loads(request.content))
            return httpx.Response(202, json={"job_id": len(payloads)})
        response: dict[str, Any] = {"status": "done", "finish_reason": "stop", "text": "one"}
        if len(payloads) == 1:
            response.update(text="", finish_reason="tool_calls", tool_calls=[NATIVE_CALL])
        return httpx.Response(200, json=response)

    monkeypatch.setattr("sniffer.broker.client.asyncio.sleep", sleep)
    broker = BrokerClient(httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    session = Session(McpResponse({"count": 1}))
    runtime = ReadAgent(
        BrokerModel(broker),
        McpReadTools(session, allowed_read_tools=READ),
        (SPEC,),
        allowed_read_tools=READ,
    )
    try:
        assert await runtime.run("scooter") == "one"
    finally:
        await broker.aclose()
    assert len(payloads) == 2
    assert payloads[0]["tools"][0]["function"]["name"] == CALL.name
    assert payloads[1]["messages"][-1]["tool_call_id"] == CALL.id
    assert len(session.calls) == 1


async def test_empty_request_never_buys_model_call() -> None:
    model = Model()
    with pytest.raises(AgentError, match="empty_request"):
        await agent(model, Gateway()).run("   ")
    assert not model.history


async def test_mcp_failure_and_cancellation_are_distinct() -> None:
    class Broken(Session):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpResponse:
            raise RuntimeError("private server detail")

    with pytest.raises(AgentError, match="mcp_call_failed") as caught:
        await McpReadTools(Broken(McpResponse(None)), allowed_read_tools=READ).call(CALL.name, {})
    assert "private" not in str(caught.value)

    class Cancelled(Session):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpResponse:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await McpReadTools(Cancelled(McpResponse(None)), allowed_read_tools=READ).call(
            CALL.name, {}
        )
