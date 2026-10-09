"""Доставка кандидатов в комнату агентов: выключена по умолчанию, курсор после успеха.

HTTP — `httpx.MockTransport`, база — подмена репозитория: сети и Postgres нет. Запросы к
настоящему Postgres — `test_room_relay_db.py`.
"""

from __future__ import annotations

import logging

import pytest
import structlog

from sniffer.config import Settings
from sniffer.notifier import room_relay
from sniffer.notifier.room_client import RoomClient, RoomError
from sniffer.notifier.room_relay import RelayConfig, RoomRelay
from tests.room_relay_support import (
    NOW,
    TOKEN,
    URL,
    FakeRepo,
    FakeRoomServer,
    candidate,
    fake_sessions,
)


@pytest.fixture(autouse=True)
def fake_repo(monkeypatch: pytest.MonkeyPatch) -> type[FakeRepo]:
    FakeRepo.pending_items = [candidate(1), candidate(2), candidate(3)]
    FakeRepo.cursor = {}
    monkeypatch.setattr(room_relay, "RoomRelayRepository", FakeRepo)
    return FakeRepo


def relay(server: FakeRoomServer, **overrides: object) -> RoomRelay:
    fields: dict[str, object] = {
        "url": URL,
        "token": TOKEN,
        "subscriptions": (1,),
        "to": "dot",
        "task_id": "sniffer-housing-watch",
    }
    fields.update(overrides)
    config = RelayConfig(**fields)  # type: ignore[arg-type]
    return RoomRelay(
        config,
        connect=lambda: RoomClient(URL, TOKEN, transport=server.transport),
        sessions=fake_sessions,
        now=lambda: NOW,
    )


def settings(**env: str) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"room_mcp_url": URL, "room_relay_subscriptions": "1"},
        {"room_token": TOKEN, "room_relay_subscriptions": "1"},
        {"room_mcp_url": URL, "room_token": TOKEN},
        {"room_mcp_url": URL, "room_token": TOKEN, "room_relay_subscriptions": " , "},
    ],
)
async def test_without_token_url_or_subscriptions_nothing_is_sent(env: dict[str, str]) -> None:
    server = FakeRoomServer()
    config = RelayConfig.from_settings(settings(**env))
    disabled = RoomRelay(
        config,
        connect=lambda: RoomClient(URL, TOKEN, transport=server.transport),
        sessions=fake_sessions,
    )
    assert not config.enabled
    assert await disabled.tick() == 0
    assert server.requests == []


def test_defaults_are_off_and_addressed_to_dot() -> None:
    config = RelayConfig.from_settings(settings())
    assert (config.enabled, config.to, config.task_id) == (False, "dot", "sniffer-housing-watch")


def test_subscription_ids_are_parsed_and_junk_is_dropped() -> None:
    config = RelayConfig.from_settings(settings(room_relay_subscriptions="1, 7,x,,12"))
    assert config.subscriptions == (1, 7, 12)


async def test_delivery_makes_the_right_json_rpc_calls_and_moves_the_cursor() -> None:
    server = FakeRoomServer()
    assert await relay(server).tick() == 3

    calls = server.calls()
    assert [c["method"] for c in calls] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
        "tools/call",
        "tools/call",
    ]
    assert all(c["jsonrpc"] == "2.0" for c in calls)
    assert "id" not in calls[1]
    assert {r.headers["authorization"] for r in server.requests} == {f"Bearer {TOKEN}"}
    # Сессия, выданная на initialize, едет в следующих запросах.
    assert [r.headers.get("mcp-session-id") for r in server.requests[1:]] == ["sess-1"] * 4

    first = calls[2]["params"]
    assert first["name"] == "room_post"
    args = first["arguments"]
    assert {k: args[k] for k in ("to", "task_id", "status", "message_id")} == {
        "to": "dot",
        "task_id": "sniffer-housing-watch",
        "status": "request",
        "message_id": "housing-cand-1",
    }
    assert [c["params"]["arguments"]["message_id"] for c in calls[2:]] == [
        "housing-cand-1",
        "housing-cand-2",
        "housing-cand-3",
    ]
    assert FakeRepo.cursor == {1: 3}


async def test_the_body_carries_link_photo_age_price_place_amenities_and_text() -> None:
    server = FakeRoomServer()
    await relay(server).tick()
    body = server.calls()[2]["params"]["arguments"]["body"]
    assert "https://t.me/c/1/1" in body
    assert "https://t.me/c/1/1?embed=1" in body
    assert "2026-10-07 (3 дн. назад)" in body
    assert "12 000 000 VND month" in body
    assert "Phuoc Hai, nha_trang" in body
    assert "Кухня: отдельная" in body
    assert "Балкон: есть" in body
    assert "Сдам студию 1, 30 м2, отдельная кухня, балкон." in body


async def test_unknown_amenities_are_said_to_be_unknown_not_guessed() -> None:
    FakeRepo.pending_items = [candidate(1, attributes={}, text=None)]
    server = FakeRoomServer()
    await relay(server).tick()
    body = server.calls()[2]["params"]["arguments"]["body"]
    assert "Кухня: по тексту / неизвестно" in body
    assert "Балкон: по тексту / неизвестно" in body
    assert "Студия, 30 м2" in body


async def test_event_stream_answers_are_understood() -> None:
    server = FakeRoomServer(stream=True)
    assert await relay(server).tick() == 3
    assert FakeRepo.cursor == {1: 3}


@pytest.mark.parametrize("mode", ["http", "is_error", "rpc_error"])
async def test_a_refusal_stops_the_pass_and_leaves_the_cursor_behind_it(mode: str) -> None:
    server = FakeRoomServer(fail_on=2, mode=mode)
    # 0, а не 1: ненулевой ответ тика гонит цикл процесса без паузы, то есть ретраем в цикле.
    assert await relay(server).tick() == 0
    assert FakeRepo.cursor == {1: 1}
    # Без ретрая в цикле: после отказа на втором посте третий не отправлялся.
    assert server.posts == 2


async def test_the_next_tick_resumes_from_the_failed_candidate_and_never_resends() -> None:
    failing = FakeRoomServer(fail_on=2, mode="http")
    await relay(failing).tick()
    healthy = FakeRoomServer()
    assert await relay(healthy).tick() == 2
    ids = [
        c["params"]["arguments"]["message_id"]
        for c in healthy.calls()
        if "arguments" in c.get("params", {})
    ]
    assert ids == ["housing-cand-2", "housing-cand-3"]
    again = FakeRoomServer()
    assert await relay(again).tick() == 0
    assert again.requests == []


async def test_an_unreachable_handshake_does_not_move_the_cursor() -> None:
    server = FakeRoomServer()
    server.handle = lambda request: (_ for _ in ()).throw(ConnectionError("boom"))  # type: ignore[method-assign]
    assert await relay(server).tick() == 0
    assert FakeRepo.cursor == {}


async def test_the_client_names_the_refusal() -> None:
    server = FakeRoomServer(fail_on=1, mode="is_error")
    async with RoomClient(URL, TOKEN, transport=server.transport) as room:
        with pytest.raises(RoomError, match="isError"):
            await room.post(to="dot", task_id="t", message_id="m", body="b")


async def test_the_token_never_reaches_the_logs(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    structlog.reset_defaults()
    for mode in ("http", "is_error", "rpc_error"):
        FakeRepo.cursor = {}
        server = FakeRoomServer(fail_on=1, mode=mode)
        service = relay(server)
        service.announce()
        await service.tick()
    out = capsys.readouterr()
    assert TOKEN not in caplog.text + out.out + out.err
    assert "SECRET" not in caplog.text + out.out + out.err


def test_the_token_is_not_in_the_repr_of_the_errors() -> None:
    assert TOKEN not in repr(RoomError("room answered HTTP 503"))
