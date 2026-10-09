"""Общие подставки для тестов доставки в комнату: кандидат, комната-заглушка, фальшивая база."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

import httpx

from sniffer.domain.room_relay import RelayCandidate

TOKEN = "sniffer:0123456789abcdef0123456789abcdef-SECRET"
URL = "https://room.example.test/mcp"
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def candidate(number: int = 1, **overrides: Any) -> RelayCandidate:
    fields: dict[str, Any] = {
        "notification_id": number,
        "subscription_id": 1,
        "score": 0.91,
        "tg_link": f"https://t.me/c/1/{number}",
        "posted_at": NOW - timedelta(days=3),
        "price_amount": Decimal("12000000"),
        "price_currency": "VND",
        "price_period": "month",
        "district": "Phuoc Hai",
        "city": "nha_trang",
        "title": "Студия у моря",
        "summary": "Студия, 30 м2",
        "attributes": {"kitchen": "separate", "balcony": True},
        "text": f"Сдам студию {number}, 30 м2, отдельная кухня, балкон.",
        "has_media": True,
        "media_count": 2,
    }
    fields.update(overrides)
    return RelayCandidate(**fields)


class FakeRoomServer:
    """Сервер комнаты на `httpx.MockTransport`: пишет запросы, отвечает по сценарию."""

    def __init__(self, *, stream: bool = False, fail_on: int | None = None, mode: str = "ok"):
        self.requests: list[httpx.Request] = []
        self.stream = stream
        self.fail_on = fail_on
        self.mode = mode
        self.posts = 0

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def calls(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        message = json.loads(request.content)
        method = message["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            return self.reply(message, {"serverInfo": {"name": "room"}}, session="sess-1")
        self.posts += 1
        if self.fail_on == self.posts:
            return self.failure(message)
        return self.reply(message, {"structuredContent": {"ok": True}, "isError": False})

    def failure(self, message: dict[str, Any]) -> httpx.Response:
        if self.mode == "http":
            return httpx.Response(503, text="down")
        if self.mode == "is_error":
            return self.reply(message, {"isError": True, "content": []})
        return self.envelope({"jsonrpc": "2.0", "id": message["id"], "error": {"message": "bad"}})

    def reply(
        self, message: dict[str, Any], result: dict[str, Any], session: str | None = None
    ) -> httpx.Response:
        return self.envelope({"jsonrpc": "2.0", "id": message["id"], "result": result}, session)

    def envelope(self, payload: dict[str, Any], session: str | None = None) -> httpx.Response:
        headers = {"Mcp-Session-Id": session} if session else {}
        if self.stream:
            text = f"event: message\ndata: {json.dumps(payload)}\n\n"
            return httpx.Response(
                200, text=text, headers={**headers, "content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=payload, headers=headers)


class FakeSession:
    async def commit(self) -> None:
        return None


@asynccontextmanager
async def fake_sessions() -> AsyncIterator[FakeSession]:
    yield FakeSession()


class FakeRepo:
    """Подмена `RoomRelayRepository`: очередь в памяти и курсор по подписке."""

    pending_items: ClassVar[list[RelayCandidate]] = []
    cursor: ClassVar[dict[int, int]] = {}

    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def pending(self, subscription_ids: Any, limit: int) -> list[RelayCandidate]:
        ids = set(subscription_ids)
        found = [
            c
            for c in self.pending_items
            if c.subscription_id in ids
            and c.notification_id > self.cursor.get(c.subscription_id, 0)
        ]
        return sorted(found, key=lambda c: c.notification_id)[:limit]

    async def advance(self, subscription_id: int, notification_id: int) -> None:
        self.cursor[subscription_id] = max(self.cursor.get(subscription_id, 0), notification_id)
