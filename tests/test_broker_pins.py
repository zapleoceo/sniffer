"""Закрепление модели за ролью: что уходит брокеру и что при отказе модели."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog

from sniffer.broker.client import (
    BrokerCapError,
    BrokerClient,
    BrokerError,
    BrokerOutputError,
    BrokerResult,
)
from sniffer.broker.pins import ROLE_BY_SCHEMA, pinned_model
from sniffer.config import Settings

SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "string"}},
    "required": ["a"],
    "additionalProperties": False,
}
LITE = "gemini/gemini-3.5-flash-lite"
# Умолчание guard пустое; роль в тестах включается явно, как её включит оператор.
GUARD_PIN = "gemini/gemini-3.6-flash"


def settings(**over: Any) -> Settings:
    return Settings(_env_file=None, broker_project_key="k", **over)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def instant_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    async def sleep(delay: float) -> None:
        pass

    monkeypatch.setattr("sniffer.broker.client.asyncio.sleep", sleep)


class FakeBroker:
    """Подделка брокера: ведёт журнал отправок, отказывает закреплённым вызовам."""

    def __init__(
        self,
        *,
        pinned_error: str | None = None,
        submit_status: int = 202,
        pinned_text: str = '{"a": "x"}',
        pinned_extra: dict[str, Any] | None = None,
        unpinned_text: str = '{"a": "x"}',
        unpinned_error: str | None = None,
    ) -> None:
        self.pinned_text = pinned_text
        self.pinned_extra = pinned_extra or {}
        self.unpinned_text = unpinned_text
        self.unpinned_error = unpinned_error
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
        if not pinned and self.unpinned_error:
            return httpx.Response(200, json={"status": "error", "error": self.unpinned_error})
        served = "gemini-3.5-flash-lite" if pinned else "deepseek-flash"
        return httpx.Response(
            200,
            json={
                "status": "done",
                "text": self.pinned_text if pinned else self.unpinned_text,
                "provider": "p",
                "model": served,
                "finish_reason": "stop",
                "request_id": len(self.submitted),
                **(self.pinned_extra if pinned else {}),
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
    client, _ = make(fake, settings(broker_model_guard=GUARD_PIN), monkeypatch)
    for schema_name in ROLE_BY_SCHEMA:
        await ask(client, schema_name)
    sent = {s["response_format"]["json_schema"]["name"]: s.get("model") for s in fake.submitted}
    assert sent == {
        "query_passport": LITE,
        "search_plan": None,
        "offer_screen": LITE,
        "catalog_facts": None,
        "listing_guard": GUARD_PIN,
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
    client, _ = make(fake, settings(broker_model_guard=GUARD_PIN), monkeypatch)
    await ask(client, "something_else")
    await client.chat([{"role": "user", "content": "hi"}], capability="chat:sales")
    assert all("model" not in s for s in fake.submitted)


async def test_chat_accepts_explicit_model(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeBroker()
    client, _ = make(fake, settings(broker_model_guard=GUARD_PIN), monkeypatch)
    await client.chat([{"role": "user", "content": "hi"}], model="gpt-oss-120b")
    assert fake.submitted[0]["model"] == "gpt-oss-120b"


async def test_job_error_on_pinned_model_retries_once_without_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeBroker(pinned_error="no provider available")
    client, accounted = make(fake, settings(broker_model_guard=GUARD_PIN), monkeypatch)
    with structlog.testing.capture_logs() as logs:
        assert await ask(client, "listing_guard") == {"a": "x"}
    assert ["model" in s for s in fake.submitted] == [True, False]
    failed = [e for e in logs if e["event"] == "broker.pinned_model_failed"]
    assert len(failed) == 1 and failed[0]["model"] == GUARD_PIN
    # Учёт хранит ту модель, что ответила на самом деле, и ровно один раз.
    assert [r.model for r in accounted] == ["deepseek-flash"]


async def test_submit_400_on_unknown_pin_also_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeBroker(submit_status=400)
    client, _ = make(fake, settings(broker_model_guard=GUARD_PIN), monkeypatch)
    assert await ask(client, "query_passport") == {"a": "x"}
    assert ["model" in s for s in fake.submitted] == [True, False]


async def test_cap_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeBroker(pinned_error="daily budget cap reached")
    client, accounted = make(fake, settings(broker_model_guard=GUARD_PIN), monkeypatch)
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

    monkeypatch.setattr(
        "sniffer.broker.client.get_settings", lambda: settings(broker_model_guard=GUARD_PIN)
    )
    client = BrokerClient(httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    with pytest.raises(BrokerError, match="boom"):
        await ask(client, "listing_guard")


@pytest.mark.parametrize(
    ("text", "extra", "reason"),
    [
        ("not json at all", {}, "invalid_json"),
        ('{"b": 1}', {}, "schema_mismatch"),
        ('{"a": "x"}', {"refusal": True}, "refusal"),
        ('{"a": "x"}', {"finish_reason": "length"}, "incomplete"),
    ],
)
async def test_invalid_pinned_answer_retries_once_without_pin(
    monkeypatch: pytest.MonkeyPatch, text: str, extra: dict[str, Any], reason: str
) -> None:
    fake = FakeBroker(pinned_text=text, pinned_extra=extra)
    client, accounted = make(fake, settings(broker_model_guard=GUARD_PIN), monkeypatch)
    with structlog.testing.capture_logs() as logs:
        assert await ask(client, "listing_guard") == {"a": "x"}
    assert ["model" in s for s in fake.submitted] == [True, False]
    invalid = [e for e in logs if e["event"] == "broker.pinned_model_invalid"]
    assert len(invalid) == 1 and invalid[0]["model"] == GUARD_PIN
    assert invalid[0]["served_model"] == "gemini-3.5-flash-lite"
    assert invalid[0]["reason"] == reason
    assert "not json" not in str(invalid[0])
    # Оба платных ответа учтены под моделью, что ответила на самом деле.
    assert [r.model for r in accounted] == ["gemini-3.5-flash-lite", "deepseek-flash"]


async def test_invalid_answer_of_the_retry_is_raised_and_not_retried_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeBroker(pinned_text="junk", unpinned_text="junk")
    client, accounted = make(fake, settings(broker_model_guard=GUARD_PIN), monkeypatch)
    with pytest.raises(BrokerOutputError):
        await ask(client, "listing_guard")
    assert len(fake.submitted) == 2 and len(accounted) == 2


async def test_cap_on_the_retry_is_raised_as_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeBroker(pinned_text="junk", unpinned_error="daily budget cap reached")
    client, accounted = make(fake, settings(broker_model_guard=GUARD_PIN), monkeypatch)
    with pytest.raises(BrokerCapError):
        await ask(client, "listing_guard")
    assert len(fake.submitted) == 2 and len(accounted) == 1


async def test_valid_pinned_answer_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeBroker()
    client, accounted = make(fake, settings(broker_model_guard=GUARD_PIN), monkeypatch)
    with structlog.testing.capture_logs() as logs:
        await ask(client, "listing_guard")
    assert len(fake.submitted) == 1 and len(accounted) == 1
    assert not [e for e in logs if e["event"] == "broker.pinned_model_invalid"]


async def test_invalid_answer_without_pin_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeBroker(pinned_text="junk", unpinned_text="junk")
    client, _ = make(fake, settings(broker_model_guard=""), monkeypatch)
    with pytest.raises(BrokerOutputError):
        await ask(client, "listing_guard")
    assert len(fake.submitted) == 1


def test_pinned_model_resolution() -> None:
    assert pinned_model(settings(broker_model_guard=GUARD_PIN), "listing_guard") == GUARD_PIN
    assert pinned_model(settings(broker_model_intake="  "), "query_passport") is None
    assert pinned_model(settings(broker_model_intake=" x/y "), "query_passport") == "x/y"
    assert pinned_model(settings(), "nope") is None


def test_only_intake_and_offer_screen_are_pinned_by_default() -> None:
    # A/B 04.10.2026: 3.6-flash на extraction и guard — валидный ответ в 4 из 17.
    cfg = settings()
    assert cfg.broker_model_planner == ""
    assert cfg.broker_model_extraction == ""
    assert cfg.broker_model_guard == ""
    assert cfg.broker_model_intake == LITE
    assert cfg.broker_model_offer_screen == LITE


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


async def test_a_pinned_failure_then_an_invalid_retry_is_two_sends_not_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Платный повтор один на всю цепочку: `chat` уже повторил, `structured` не повторяет."""
    fake = FakeBroker(pinned_error="no provider available", unpinned_text="not json at all")
    client, _ = make(fake, settings(broker_model_guard=GUARD_PIN), monkeypatch)
    with pytest.raises(BrokerOutputError):
        await ask(client, "listing_guard")
    assert ["model" in s for s in fake.submitted] == [True, False]


async def test_an_invalid_pinned_answer_still_gets_exactly_one_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeBroker(pinned_text="not json at all", unpinned_text="still not json")
    client, _ = make(fake, settings(broker_model_guard=GUARD_PIN), monkeypatch)
    with pytest.raises(BrokerOutputError):
        await ask(client, "listing_guard")
    assert ["model" in s for s in fake.submitted] == [True, False]
