"""Закрепление модели за ролью: что уходит брокеру и что при отказе модели."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog

from sniffer.broker.client import BrokerClient, BrokerError, BrokerResult
from sniffer.broker.pins import ROLE_BY_SCHEMA, pinned_model
from sniffer.config import Settings

SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "string"}},
    "required": ["a"],
    "additionalProperties": False,
}
LITE = "gemini/gemini-3.5-flash-lite"


def settings(**over: Any) -> Settings:
    return Settings(_env_file=None, broker_project_key="k", **over)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def instant_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    async def sleep(delay: float) -> None:
        pass

    monkeypatch.setattr("sniffer.broker.client.asyncio.sleep", sleep)


class FakeBroker:
    """Подделка брокера: ведёт журнал отправок, отказывает закреплённым вызовам."""

    def __init__(self, *, pinned_error: str | None = None, submit_status: int = 202) -> None:
        self.submitted: list[dict[str, Any]] = []
        self.pinned_error = pinned_error
        self.submit_status = submit_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.content)
            self.submitted.append(body)
            if "model" in body and self.submit_status >= 400:
                return httpx.Response(self.submit_status, text="unknown model")
            return httpx.Response(202, json={"job_id": len(self.submitted)})
        pinned = "model" in self.submitted[-1]
        if pinned and self.pinned_error:
            return httpx.Response(200, json={"status": "error", "error": self.pinned_error})
        served = "gemini-3.5-flash-lite" if pinned else "deepseek-flash"
        return httpx.Response(
            200,
            json={
                "status": "done",
                "text": '{"a": "x"}',
                "provider": "p",
                "model": served,
                "finish_reason": "stop",
                "request_id": len(self.submitted),
            },
        )


def make(
    fake: FakeBroker, cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> tuple[BrokerClient, list[BrokerResult]]:
    monkeypatch.setattr("sniffer.broker.client.get_settings", lambda: cfg)
    accounted: list[BrokerResult] = []

    async def sink(capability: str, result: BrokerResult) -> None:
        accounted.append(result)

    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return BrokerClient(http, usage=sink), accounted


async def ask(client: BrokerClient, schema_name: str) -> dict[str, Any]:
    return await client.structured("p", schema=SCHEMA, schema_name=schema_name)


async def test_each_role_sends_its_own_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeBroker()
    client, _ = make(fake, settings(), monkeypatch)
    for schema_name in ROLE_BY_SCHEMA:
        await ask(client, schema_name)
    sent = {s["response_format"]["json_schema"]["name"]: s["model"] for s in fake.submitted}
    assert sent == {
        "query_passport": LITE,
        "search_plan": LITE,
        "offer_screen": LITE,
        "catalog_facts": LITE,
        "listing_guard": "gemini/gemini-3.6-flash",
    }


async def test_empty_setting_sends_no_model_field(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeBroker()
    client, _ = make(fake, settings(broker_model_guard=""), monkeypatch)
    await ask(client, "listing_guard")
    assert "model" not in fake.submitted[0]


async def test_unknown_schema_and_plain_chat_are_not_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeBroker()
    client, _ = make(fake, settings(), monkeypatch)
    await ask(client, "something_else")
    await client.chat([{"role": "user", "content": "hi"}], capability="chat:sales")
    assert all("model" not in s for s in fake.submitted)


async def test_chat_accepts_explicit_model(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeBroker()
    client, _ = make(fake, settings(), monkeypatch)
    await client.chat([{"role": "user", "content": "hi"}], model="gpt-oss-120b")
    assert fake.submitted[0]["model"] == "gpt-oss-120b"


async def test_job_error_on_pinned_model_retries_once_without_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeBroker(pinned_error="no provider available")
    client, accounted = make(fake, settings(), monkeypatch)
    with structlog.testing.capture_logs() as logs:
        assert await ask(client, "listing_guard") == {"a": "x"}
    assert ["model" in s for s in fake.submitted] == [True, False]
    failed = [e for e in logs if e["event"] == "broker.pinned_model_failed"]
    assert len(failed) == 1 and failed[0]["model"] == "gemini/gemini-3.6-flash"
    # Учёт хранит ту модель, что ответила на самом деле, и ровно один раз.
    assert [r.model for r in accounted] == ["deepseek-flash"]


async def test_submit_400_on_unknown_pin_also_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeBroker(submit_status=400)
    client, _ = make(fake, settings(), monkeypatch)
    assert await ask(client, "query_passport") == {"a": "x"}
    assert ["model" in s for s in fake.submitted] == [True, False]


async def test_cap_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeBroker(pinned_error="daily budget cap reached")
    client, accounted = make(fake, settings(), monkeypatch)
    with pytest.raises(BrokerError):
        await ask(client, "listing_guard")
    assert len(fake.submitted) == 1 and accounted == []


async def test_failure_of_the_unpinned_retry_is_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": 1})
        return httpx.Response(200, json={"status": "error", "error": "boom"})

    monkeypatch.setattr("sniffer.broker.client.get_settings", lambda: settings())
    client = BrokerClient(httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    with pytest.raises(BrokerError, match="boom"):
        await ask(client, "listing_guard")


def test_pinned_model_resolution() -> None:
    assert pinned_model(settings(), "listing_guard") == "gemini/gemini-3.6-flash"
    assert pinned_model(settings(broker_model_intake="  "), "query_passport") is None
    assert pinned_model(settings(broker_model_intake=" x/y "), "query_passport") == "x/y"
    assert pinned_model(settings(), "nope") is None


def test_role_table_matches_schema_names_used_in_code() -> None:
    src = Path(__file__).resolve().parents[1] / "src" / "sniffer"
    used = {
        m
        for f in src.rglob("*.py")
        for m in re.findall(r'schema_name="(\w+)"', f.read_text(encoding="utf-8"))
    }
    assert used == set(ROLE_BY_SCHEMA)
    for role in ROLE_BY_SCHEMA.values():
        assert hasattr(Settings(_env_file=None), f"broker_model_{role}")  # type: ignore[call-arg]
