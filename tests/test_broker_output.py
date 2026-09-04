"""Exercise the real paid-call boundary using HTTP fixtures, never a live model."""

from __future__ import annotations

import json
import traceback
from typing import Any

import httpx
import pytest
from jsonschema import SchemaError

from sniffer.broker.client import BrokerClient, BrokerError, BrokerOutputError, BrokerResult
from sniffer.broker.contracts import UsageSink

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"action": {"enum": ["search"]}, "limit": {"type": "integer"}},
    "required": ["action", "limit"],
    "additionalProperties": False,
}
VALID = '{"action":"search","limit":3}'


@pytest.fixture(autouse=True)
def no_poll_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    async def sleep(delay: float) -> None:
        pass

    monkeypatch.setattr("sniffer.broker.client.asyncio.sleep", sleep)


def client_for(
    body: dict[str, Any],
    requests: list[httpx.Request],
    sink: UsageSink | None = None,
) -> BrokerClient:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": 9})
        return httpx.Response(
            200,
            json={
                "status": "done",
                "provider": "test",
                "request_id": 17,
                **body,
            },
        )

    return BrokerClient(httpx.AsyncClient(transport=httpx.MockTransport(handle)), usage=sink)


@pytest.mark.parametrize("finish", [None, "stop", "STOP", "end_turn", "completed"])
async def test_validated_object_and_schema_sent(finish: str | None) -> None:
    requests: list[httpx.Request] = []
    client = client_for({"text": VALID, "finish_reason": finish}, requests)
    try:
        result = await client.structured("private prompt", schema=SCHEMA, schema_name="action")
    finally:
        await client.aclose()
    assert result == {"action": "search", "limit": 3}
    payload = json.loads(requests[0].content)
    assert payload["response_format"]["json_schema"] == {
        "name": "action",
        "strict": True,
        "schema": SCHEMA,
    }
    assert len(requests) == 2


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ('{"action":', "invalid_json"),
        ("```json\n{}\n```", "invalid_json"),
        ("{} trailing", "invalid_json"),
        ("[]", "not_object"),
        ("null", "not_object"),
        ('"secret model text"', "not_object"),
        ('{"action":"search","limit":NaN}', "nonfinite_number"),
        ('{"action":"search","limit":Infinity}', "nonfinite_number"),
        ('{"action":"search","limit":-Infinity}', "nonfinite_number"),
        ('{"action":"search","limit":1e999}', "nonfinite_number"),
        ('{"action":"search","limit":2,"limit":3}', "duplicate_key"),
        ('{"action":"delete","limit":3}', "schema_mismatch"),
        ('{"action":"search","limit":"3"}', "schema_mismatch"),
        ('{"action":"search","limit":true}', "schema_mismatch"),
        ('{"action":"search"}', "schema_mismatch"),
        ('{"action":"search","limit":3,"secret":"secret model text"}', "schema_mismatch"),
    ],
)
async def test_rejection_is_typed_private_and_never_retried(text: str, reason: str) -> None:
    requests: list[httpx.Request] = []
    client = client_for({"text": text}, requests)
    prompt = "private prompt"
    try:
        with pytest.raises(BrokerOutputError) as caught:
            await client.structured(prompt, schema=SCHEMA, schema_name="action")
    finally:
        await client.aclose()
    error = caught.value
    assert isinstance(error, BrokerError)
    assert error.reason == reason
    assert (error.provider, error.request_id, error.job_id) == ("test", 17, 9)
    diagnostic = "".join(traceback.format_exception(error))
    assert "secret model text" not in diagnostic
    assert "private prompt" not in diagnostic
    assert len(requests) == 2


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        ({"finish_reason": "length"}, "incomplete"),
        ({"finish_reason": "MAX_TOKENS"}, "incomplete"),
        ({"finish_reason": "content_filter"}, "incomplete"),
        ({"finish_reason": "unknown"}, "incomplete"),
        ({"finish_reason": 5}, "incomplete"),
        ({"refusal": "private refusal explanation"}, "refusal"),
    ],
)
async def test_even_valid_json_rejected_when_generation_not_complete(
    metadata: dict[str, Any],
    reason: str,
) -> None:
    requests: list[httpx.Request] = []
    client = client_for({"text": VALID, **metadata}, requests)
    try:
        with pytest.raises(BrokerOutputError) as caught:
            await client.structured("x", schema=SCHEMA, schema_name="action")
    finally:
        await client.aclose()
    assert caught.value.reason == reason

    assert "private refusal" not in str(caught.value)
    assert len(requests) == 2


@pytest.mark.parametrize("text", [None, 42, '{"limit":' + "9" * 5000 + "}"])
async def test_malformed_text_envelope_and_excessive_integer_are_typed(text: Any) -> None:
    client = client_for({"text": text}, [])
    try:
        with pytest.raises(BrokerOutputError) as caught:
            await client.structured("x", schema=SCHEMA, schema_name="action")
    finally:
        await client.aclose()
    assert caught.value.reason == "invalid_json"


async def test_rejected_paid_output_is_still_accounted_once() -> None:
    recorded: list[BrokerResult] = []

    async def sink(capability: str, result: BrokerResult) -> None:
        recorded.append(result)

    client = client_for({"text": "broken", "cost_usd": 0.01}, [], sink)
    try:
        with pytest.raises(BrokerOutputError):
            await client.structured("x", schema=SCHEMA, schema_name="action")
    finally:
        await client.aclose()
    assert len(recorded) == 1
    assert recorded[0].cost_usd == 0.01


async def test_invalid_schema_fails_before_paid_request() -> None:
    requests: list[httpx.Request] = []
    client = client_for({"text": VALID}, requests)
    try:
        with pytest.raises(SchemaError):
            await client.structured("x", schema={"type": "nonsense"}, schema_name="bad")
    finally:
        await client.aclose()
    assert not requests


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ('{"nested":{"x":1,"x":2}}', "duplicate_key"),
        ('{"nested":[1e999]}', "nonfinite_number"),
    ],
)
async def test_nested_invalid_values_are_not_hidden_by_permissive_schema(
    text: str,
    reason: str,
) -> None:
    requests: list[httpx.Request] = []
    client = client_for({"text": text}, requests)
    try:
        with pytest.raises(BrokerOutputError) as caught:
            await client.structured("x", schema={"type": "object"}, schema_name="nested")
    finally:
        await client.aclose()
    assert caught.value.reason == reason
