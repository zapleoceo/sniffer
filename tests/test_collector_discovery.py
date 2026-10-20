"""Боевой проход очереди вступления, без сети и без настоящего Telegram."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from structlog.testing import capture_logs
from telethon import errors

from sniffer.collector.discovery import (
    TELEGRAM_FLOOD_BACKOFF_CAP_S,
    TELEGRAM_POLL_INTERVAL_S,
    DiscoveryRunner,
)
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
    order: list[str] | None = None

    async def sync(self) -> int:
        self.calls += 1
        if self.order is not None:
            self.order.append("sync")
        return self.synced


@dataclass
class FakeBackfill:
    inserted: int = 0
    calls: int = 0
    order: list[str] | None = None

    async def run(self) -> int:
        self.calls += 1
        if self.order is not None:
            self.order.append("backfill")
        return self.inserted


@dataclass
class FakeLiveness:
    order: list[str] | None = None

    async def run(self) -> int:
        if self.order is not None:
            self.order.append("liveness")
        return 0


def _settings() -> Settings:
    return Settings(tg_api_id=1, tg_api_hash="hash", tg_session="session")


async def test_cycle_connects_joins_once_retries_mutes_and_disconnects() -> None:
    client = FakeClient()
    joiner = FakeJoiner(
        DiscoveredChat(tg_id=-100123, username="nha_flea", title="Барахолка", city="nha_trang"),
        muted=2,
    )
    history = FakeHistory(synced=7)
    backfill = FakeBackfill(inserted=11)
    runner = DiscoveryRunner(
        _settings(),
        client_factory=lambda _settings: cast(TelegramJoiner, client),
        joiner_factory=lambda _client: joiner,
        history_factory=lambda _client: history,
        backfill_factory=lambda _client: backfill,
        liveness_factory=lambda _client: FakeLiveness(),
    )

    assert await runner.tick() == 3
    assert (client.connected, client.disconnected) == (1, 1)
    assert (joiner.mute_calls, joiner.join_calls) == (1, 1)
    assert (history.calls, backfill.calls) == (1, 1)


async def test_the_archive_is_read_after_the_fresh_posts_never_before() -> None:
    """Свежее объявление клиенту нужнее вчерашнего, а добор длинный.

    Порядок держится тестом, а не памятью автора: поменяй местами две строки в
    `tick()` — и на чате с архивом в двадцать восемь тысяч сообщений свежие
    посты будут ждать конца добора, то есть часами.
    """
    order: list[str] = []
    runner = DiscoveryRunner(
        _settings(),
        client_factory=lambda _settings: cast(TelegramJoiner, FakeClient()),
        joiner_factory=lambda _client: FakeJoiner(None),
        history_factory=lambda _client: FakeHistory(order=order),
        backfill_factory=lambda _client: FakeBackfill(order=order),
        liveness_factory=lambda _client: FakeLiveness(order=order),
    )

    await runner.tick()

    assert order == ["sync", "backfill", "liveness"], "живость каталога — последней в проходе"


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
        backfill_factory=lambda _client: FakeBackfill(),
        liveness_factory=lambda _client: FakeLiveness(),
        owner_alert=lambda _settings, _error: _record_alert(alerts, _error),
    )

    with capture_logs() as logs:
        assert await runner.tick() == 0
        assert await runner.tick() == 0

    assert called is False
    assert client.disconnected == 2
    assert alerts == ["ValueError"]
    assert any(event["event"] == "collector.telegram_unavailable" for event in logs)


async def test_flood_wait_backs_off_telegram_but_does_not_starve_public_previews() -> None:
    """Живой отказ 20.10.2026: история падала раньше независимого preview scan."""

    class FloodedHistory(FakeHistory):
        async def sync(self) -> int:
            self.calls += 1
            raise errors.FloodWaitError(request=None, capture=3)

    client = FakeClient()
    backfill = FakeBackfill()
    liveness = FakeLiveness(order=[])
    previews: list[Settings] = []
    settings = _settings()
    now = [1000.0]
    history = FloodedHistory()

    async def scan(settings: Settings) -> int:
        previews.append(settings)
        return 4

    runner = DiscoveryRunner(
        settings,
        client_factory=lambda _settings: cast(TelegramJoiner, client),
        joiner_factory=lambda _client: FakeJoiner(None),
        history_factory=lambda _client: history,
        backfill_factory=lambda _client: backfill,
        liveness_factory=lambda _client: liveness,
        preview_scanner=scan,
        clock=lambda: now[0],
    )

    with capture_logs() as logs:
        assert await runner.tick() == 0
        now[0] += TELEGRAM_POLL_INTERVAL_S / 2
        assert await runner.tick() == 0
        now[0] += TELEGRAM_POLL_INTERVAL_S / 2
        assert await runner.tick() == 0
        now[0] += TELEGRAM_POLL_INTERVAL_S * 2 - 1
        assert await runner.tick() == 0

    assert (client.connected, client.disconnected) == (2, 2)
    assert history.calls == 2, "повторный FloodWait не вызывает запрос до конца backoff"
    assert backfill.calls == 0
    assert liveness.order == []
    assert previews == [settings] * 4, "независимые web-превью работают и в паузе"
    waits = [event for event in logs if event["event"] == "collector.telegram_flood_wait"]
    assert [event["seconds"] for event in waits] == [3, 3]
    assert [event["pause_seconds"] for event in waits] == [900.0, 1800.0]


async def test_flood_wait_honours_long_server_wait_before_retrying() -> None:
    class FloodedHistory(FakeHistory):
        async def sync(self) -> int:
            self.calls += 1
            raise errors.FloodWaitError(request=None, capture=3600)

    now = [5000.0]
    history = FloodedHistory()
    previews = 0

    async def scan(_settings: Settings) -> int:
        nonlocal previews
        previews += 1
        return 0

    runner = DiscoveryRunner(
        _settings(),
        client_factory=lambda _settings: cast(TelegramJoiner, FakeClient()),
        joiner_factory=lambda _client: FakeJoiner(None),
        history_factory=lambda _client: history,
        backfill_factory=lambda _client: FakeBackfill(),
        liveness_factory=lambda _client: FakeLiveness(),
        preview_scanner=scan,
        clock=lambda: now[0],
    )

    assert await runner.tick() == 0
    now[0] += 3599
    assert await runner.tick() == 0
    assert history.calls == 1, "серверный FloodWait длиннее poll interval соблюдается целиком"
    now[0] += 1
    assert await runner.tick() == 0
    assert history.calls == 2
    assert previews == 3


async def test_successful_telegram_tick_resets_the_flood_backoff() -> None:
    class RecoveringHistory(FakeHistory):
        async def sync(self) -> int:
            self.calls += 1
            if self.calls != 2:
                raise errors.FloodWaitError(request=None, capture=3)
            return 0

    now = [1000.0]
    history = RecoveringHistory()
    runner = DiscoveryRunner(
        _settings(),
        client_factory=lambda _settings: cast(TelegramJoiner, FakeClient()),
        joiner_factory=lambda _client: FakeJoiner(None),
        history_factory=lambda _client: history,
        backfill_factory=lambda _client: FakeBackfill(),
        liveness_factory=lambda _client: FakeLiveness(),
        preview_scanner=lambda _settings: _zero(),
        clock=lambda: now[0],
    )

    with capture_logs() as logs:
        assert await runner.tick() == 0
        now[0] += TELEGRAM_POLL_INTERVAL_S
        assert await runner.tick() == 0
        assert await runner.tick() == 0

    waits = [event for event in logs if event["event"] == "collector.telegram_flood_wait"]
    assert [event["pause_seconds"] for event in waits] == [900.0, 900.0]


async def test_repeated_flood_wait_backoff_is_capped_at_twelve_hours() -> None:
    class FloodedHistory(FakeHistory):
        async def sync(self) -> int:
            self.calls += 1
            raise errors.FloodWaitError(request=None, capture=1)

    now = [1000.0]
    history = FloodedHistory()
    runner = DiscoveryRunner(
        _settings(),
        client_factory=lambda _settings: cast(TelegramJoiner, FakeClient()),
        joiner_factory=lambda _client: FakeJoiner(None),
        history_factory=lambda _client: history,
        backfill_factory=lambda _client: FakeBackfill(),
        liveness_factory=lambda _client: FakeLiveness(),
        preview_scanner=lambda _settings: _zero(),
        clock=lambda: now[0],
    )

    with capture_logs() as logs:
        for pause in (900.0, 1800.0, 3600.0, 7200.0, 14400.0, 28800.0, 43200.0, 43200.0):
            assert await runner.tick() == 0
            now[0] += pause

    waits = [event for event in logs if event["event"] == "collector.telegram_flood_wait"]
    assert [event["pause_seconds"] for event in waits] == [
        900.0,
        1800.0,
        3600.0,
        7200.0,
        14400.0,
        28800.0,
        TELEGRAM_FLOOD_BACKOFF_CAP_S,
        TELEGRAM_FLOOD_BACKOFF_CAP_S,
    ]


async def _zero() -> int:
    return 0


async def _record_alert(alerts: list[str], error: str) -> None:
    alerts.append(error)
