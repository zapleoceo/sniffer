"""Боевой проход очереди вступления, без сети и без настоящего Telegram."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from structlog.testing import capture_logs

from sniffer.collector.discovery import DiscoveryRunner
from sniffer.config import Settings
from sniffer.sources.telegram_discover_reference import DiscoveredChat, TelegramJoiner


@dataclass
class FakeClient:
    connected: int = 0
    disconnected: int = 0
    fail_connect: bool = False

    async def connect(self) -> None:
        self.connected += 1
        if self.fail_connect:
            raise ValueError("сессия отозвана")

    async def disconnect(self) -> None:
        self.disconnected += 1


@dataclass
class FakeJoiner:
    joined: DiscoveredChat | None
    muted: int = 0
    mute_calls: int = 0
    join_calls: int = 0

    async def retry_mutes(self) -> int:
        self.mute_calls += 1
        return self.muted

    async def join_next(self) -> DiscoveredChat | None:
        self.join_calls += 1
        return self.joined


@dataclass
class FakeHistory:
    synced: int = 0
    calls: int = 0

    async def sync(self) -> int:
        self.calls += 1
        return self.synced


def _settings() -> Settings:
    return Settings(tg_api_id=1, tg_api_hash="hash", tg_session="session")


async def test_cycle_connects_joins_once_retries_mutes_and_disconnects() -> None:
    client = FakeClient()
    joiner = FakeJoiner(
        DiscoveredChat(tg_id=-100123, username="nha_flea", title="Барахолка", city="nha_trang"),
        muted=2,
    )
    history = FakeHistory(synced=7)
    runner = DiscoveryRunner(
        _settings(),
        client_factory=lambda _settings: cast(TelegramJoiner, client),
        joiner_factory=lambda _client: joiner,
        history_factory=lambda _client: history,
    )

    assert await runner.tick() == 3
    assert (client.connected, client.disconnected) == (1, 1)
    assert (joiner.mute_calls, joiner.join_calls, history.calls) == (1, 1, 1)


async def test_unavailable_telegram_does_not_enter_the_queue_or_retry_in_a_loop() -> None:
    client = FakeClient(fail_connect=True)
    called = False
    alerts: list[str] = []

    def make_joiner(_client: object) -> FakeJoiner:
        nonlocal called
        called = True
        return FakeJoiner(None)

    runner = DiscoveryRunner(
        _settings(),
        client_factory=lambda _settings: cast(TelegramJoiner, client),
        joiner_factory=make_joiner,
        history_factory=lambda _client: FakeHistory(),
        owner_alert=lambda _settings, _error: _record_alert(alerts, _error),
    )

    with capture_logs() as logs:
        assert await runner.tick() == 0
        assert await runner.tick() == 0

    assert called is False
    assert client.disconnected == 2
    assert alerts == ["ValueError"]
    assert any(event["event"] == "collector.telegram_unavailable" for event in logs)


async def _record_alert(alerts: list[str], error: str) -> None:
    alerts.append(error)
