"""Exercise the subscription prefix and its old fallback over mock HTTP only."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Final

import httpx
import pytest

from sniffer.broker.client import BrokerCapError, BrokerClient, BrokerError
from sniffer.config import Settings

TERRA: Final = "codex-sub/gpt-6.1-terra"
LITE = "gemini/gemini-3.5-flash-lite"
SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}


class Broker:
    def __init__(self, primary: str = "done", old: str = "done") -> None:
        self.primary, self.old = primary, old
        self.jobs: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self.keys: list[str | None] = []
        self.cancelled = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if path.endswith("/cancel"):
            self.cancelled = True
            return httpx.Response(200, json={"state": "cancel_requested"})
        if request.method == "POST":
            body = json.loads(request.content)
            self.jobs.append(body)
            self.keys.append(request.headers.get("Idempotency-Key"))
            if body.get("model") == TERRA:
                if self.primary == "submit_timeout":
                    raise httpx.ReadTimeout("ambiguous submit", request=request)
                if self.primary.isdigit():
                    return httpx.Response(int(self.primary), text="rejected")
            return httpx.Response(202, json={"job_id": len(self.jobs)})
        job = int(path.rsplit("/", 1)[1])
        primary = self.jobs[job - 1].get("model") == TERRA
        mode = self.primary if primary else self.old
        if primary and mode == "tool_wait":
            return httpx.Response(
                200,
                json={
                    "status": "cancelled" if self.cancelled else "pending",
                    "tool_calls": [{"id": "call1"}],
                    "poll_after_s": None,
                },
            )
        if primary and mode in {"timeout", "cancel_pending"}:
            if not self.cancelled:
                raise httpx.ReadTimeout("poll timeout", request=request)
            return httpx.Response(
                200,
                json={
                    "status": "cancelled" if mode == "timeout" else "running",
                },
            )
        if mode in {"error", "cap"}:
            return httpx.Response(
                200,
                json={
                    "status": "error",
                    "error": ("daily budget cap reached" if mode == "cap" else "unavailable"),
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "done",
                "text": "" if mode == "empty" else ("junk" if mode == "invalid" else '{"a":"ok"}'),
                "provider": "codex-sub" if primary else "gemini",
                "model": TERRA if primary else "gemini-3.5-flash-lite",
                "billing_type": "subscription" if primary else "api",
                "finish_reason": "stop",
                "refusal": mode == "refusal",
            },
        )


def client(fake: Broker, monkeypatch: pytest.MonkeyPatch, **settings: Any) -> BrokerClient:
    cfg = Settings(
        _env_file=None,
        broker_primary_model=TERRA,  # type: ignore[call-arg]
        broker_primary_cancel_timeout_s=1,
        **settings,
    )
    monkeypatch.setattr("sniffer.broker.client.get_settings", lambda: cfg)
    return BrokerClient(httpx.AsyncClient(transport=httpx.MockTransport(fake)))


async def ask(c: BrokerClient, schema_name: str = "query_passport") -> dict[str, Any]:
    return await c.structured("private prompt", schema=SCHEMA, schema_name=schema_name)


async def test_terra_success_is_only_job(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Broker()
    assert await ask(client(fake, monkeypatch)) == {"a": "ok"}
    assert len(fake.jobs) == 1
    assert fake.keys[0]
    assert "max_tokens" not in fake.jobs[0] and "temperature" not in fake.jobs[0]
    assert fake.jobs[0]["no_shell"] is True


@pytest.mark.parametrize("mode", ["error", "empty", "invalid", "refusal", "400", "503"])
async def test_one_primary_then_unchanged_role_pin(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    fake = Broker(mode)
    assert await ask(client(fake, monkeypatch)) == {"a": "ok"}
    assert [j.get("model") for j in fake.jobs] == [TERRA, LITE]
    assert fake.jobs[1]["max_tokens"] == 2048
    assert fake.jobs[1]["temperature"] == 0.1


async def test_old_chain_not_reentered_through_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Broker("invalid", "invalid")
    with pytest.raises(BrokerError):
        await ask(client(fake, monkeypatch))
    assert [j.get("model") for j in fake.jobs] == [TERRA, LITE, None]


async def test_unpinned_role_retains_old_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Broker("error")
    assert await ask(client(fake, monkeypatch), "search_plan") == {"a": "ok"}
    assert [j.get("model") for j in fake.jobs] == [TERRA, None]


@pytest.mark.parametrize("mode", ["401", "403", "500", "submit_timeout", "cancel_pending"])
async def test_uncertain_or_denied_never_starts_paid_fallback(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    fake = Broker(mode)
    with pytest.raises(BrokerError):
        await ask(client(fake, monkeypatch))
    assert len(fake.jobs) == 1


async def test_cap_remains_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Broker("cap")
    with pytest.raises(BrokerCapError):
        await ask(client(fake, monkeypatch))
    assert len(fake.jobs) == 1


async def test_timeout_confirms_cancellation_before_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Broker("timeout")
    assert await ask(client(fake, monkeypatch)) == {"a": "ok"}
    assert fake.calls[:5] == [
        ("POST", "/v1/jobs"),
        ("GET", "/v1/jobs/1"),
        ("POST", "/v1/jobs/1/cancel"),
        ("GET", "/v1/jobs/1"),
        ("POST", "/v1/jobs"),
    ]


async def test_plain_chat_and_tools_take_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Broker()
    c = client(fake, monkeypatch)
    await c.chat([{"role": "user", "content": "hello"}])
    tools = [{"type": "function", "function": {"name": "search", "parameters": {}}}]
    await c.chat([{"role": "user", "content": "hello"}], tools=tools)
    assert [j.get("model") for j in fake.jobs] == [TERRA, TERRA]
    assert fake.jobs[1]["tools"] == tools and "no_shell" not in fake.jobs[1]


async def test_wall_deadline_cancels_known_job(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Broker()

    async def slow(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and not fake.cancelled:
            await asyncio.sleep(10)
        return fake(request)

    c = client(fake, monkeypatch, broker_primary_timeout_s=1)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    assert await ask(c) == {"a": "ok"}
    assert fake.cancelled
    assert [j.get("model") for j in fake.jobs] == [TERRA, LITE]


async def test_caller_cancellation_cancels_job_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Broker()
    polling = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and not fake.cancelled:
            polling.set()
            await asyncio.sleep(10)
        return fake(request)

    c = client(fake, monkeypatch)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    task = asyncio.create_task(ask(c))
    await polling.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.cancelled and len(fake.jobs) == 1


async def test_pending_tool_callback_is_cancelled_before_native_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Broker("tool_wait")
    c = client(fake, monkeypatch)
    result = await c.chat(
        [{"role": "user", "content": "hello"}],
        tools=[{"type": "function", "function": {"name": "search", "parameters": {}}}],
    )
    assert fake.cancelled and result.provider == "gemini"
    assert [j.get("model") for j in fake.jobs] == [TERRA, None]
    assert not any(path.endswith("/tool-results") for _, path in fake.calls)


async def test_billing_mismatch_stops_without_paid_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Broker()

    def untrusted(request: httpx.Request) -> httpx.Response:
        response = fake(request)
        if request.method == "GET":
            return httpx.Response(200, json={**response.json(), "billing_type": "api"})
        return response

    c = client(fake, monkeypatch)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(untrusted))
    with pytest.raises(BrokerError, match="not attested"):
        await ask(c)
    assert len(fake.jobs) == 1
