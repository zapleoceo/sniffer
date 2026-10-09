"""Клиент комнаты агентов vera-room: MCP по streamable HTTP, JSON-RPC 2.0.

Протокол сверен с сервером комнаты (vera3/services/mcp, `room_tools.room_post`): сервер
stateless и отвечает JSON, но клиент допускает и `text/event-stream`, и `Mcp-Session-Id`.
Один сеанс = `initialize` → `notifications/initialized` → `tools/call`.

Токен живёт только в заголовке `Authorization`. В тексты ошибок и в лог он не попадает: ошибка
несёт код ответа и сообщение JSON-RPC, но не заголовки запроса и не URL с учётными данными.
"""

from __future__ import annotations

import json
from types import TracebackType
from typing import Any, Self

import httpx

PROTOCOL_VERSION = "2025-03-26"
CLIENT_NAME = "sniffer-room-relay"
SESSION_HEADER = "Mcp-Session-Id"
ERROR_TEXT_CHARS = 200


class RoomError(RuntimeError):
    """Комната не приняла запрос: сеть, HTTP, JSON-RPC error или `isError` в результате."""


def extract_message(response: httpx.Response) -> dict[str, Any]:
    """Тело ответа JSON-RPC: из `application/json` или из `data:` строки event-stream."""
    kind = response.headers.get("content-type", "")
    text = response.text
    if "text/event-stream" in kind:
        payloads = [
            line[5:].strip() for line in text.splitlines() if line.startswith("data:") and line[5:]
        ]
        if not payloads:
            raise RoomError("room answered an event-stream without data")
        text = payloads[-1]
    try:
        message = json.loads(text)
    except ValueError as exc:
        raise RoomError("room answered something that is not JSON") from exc
    if not isinstance(message, dict):
        raise RoomError("room answered JSON that is not an object")
    return message


class RoomClient:
    """Сеанс с комнатой. Транспорт подменяется в тестах (`httpx.MockTransport`)."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=timeout_s,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
        self._url = url
        self._next_id = 0
        self._session_id: str | None = None

    async def __aenter__(self) -> Self:
        await self.handshake()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    async def handshake(self) -> None:
        reply = await self.call(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": "1"},
            },
        )
        if "result" not in reply:
            raise RoomError("room initialize returned no result")
        await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def post(
        self, *, to: str, task_id: str, message_id: str, body: str, status: str = "request"
    ) -> None:
        """`room_post`; повтор с тем же `message_id` комната отдаёт как deduped — это успех."""
        arguments = {"to": to, "task_id": task_id, "status": status, "message_id": message_id}
        reply = await self.call(
            "tools/call", {"name": "room_post", "arguments": {**arguments, "body": body}}
        )
        result = reply.get("result")
        if not isinstance(result, dict):
            raise RoomError("room_post returned no result")
        if result.get("isError"):
            raise RoomError("room_post refused: isError")
        structured = result.get("structuredContent")
        if isinstance(structured, dict) and structured.get("ok") is False:
            raise RoomError("room_post refused: ok=false")

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        response = await self.send(payload)
        message = extract_message(response)
        error = message.get("error")
        if error is not None:
            detail = error.get("message") if isinstance(error, dict) else error
            raise RoomError(f"{method}: JSON-RPC error: {str(detail)[:ERROR_TEXT_CHARS]}")
        return message

    async def send(self, payload: dict[str, Any]) -> httpx.Response:
        headers = {SESSION_HEADER: self._session_id} if self._session_id else {}
        try:
            response = await self._http.post(self._url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise RoomError(f"room unreachable: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise RoomError(f"room answered HTTP {response.status_code}")
        if self._session_id is None:
            self._session_id = response.headers.get(SESSION_HEADER)
        return response
