"""Поведение процесса без настроек и на остановке.

Главная проверка здесь одна: контейнер без секрета не должен уходить в
crash-loop. Перезапускающийся процесс выглядит как сломанный деплой, хотя на
деле просто не заведён токен, — и отличить одно от другого можно только по
тому, жив он или падает.
"""

from __future__ import annotations

import asyncio

import pytest
from structlog.testing import capture_logs

from sniffer.bot import app as bot_app
from sniffer.collector import __main__ as collector_main
from sniffer.config import Settings
from sniffer.notifier import __main__ as notifier_main
from sniffer.runtime.service import Service, idle_loop, serve, sleep_or_stop
from sniffer.worker import __main__ as worker_main

# Быстрее реального часа ожидания, смысл проверки тот же.
FAST_RECHECK_S = 0.01


async def _never_runs(_stop: asyncio.Event) -> None:
    raise AssertionError("процесс без настроек работать не должен")


async def test_process_without_token_waits_instead_of_crashing() -> None:
    stop = asyncio.Event()
    service = Service(name="bot", requires=lambda _s: ["BOT_TOKEN"], run=_never_runs)

    with capture_logs() as logs:
        waiting = asyncio.create_task(serve(service, stop, recheck_s=FAST_RECHECK_S))
        await asyncio.sleep(FAST_RECHECK_S * 5)
        assert not waiting.done(), "процесс обязан ждать, а не заканчиваться"
        stop.set()
        await asyncio.wait_for(waiting, timeout=1)

    complaints = [entry for entry in logs if entry["event"] == "service.not_configured"]
    assert complaints, "молчащий процесс неотличим от сломанного"
    assert complaints[0]["missing"] == ["BOT_TOKEN"]
    assert "не настроен: нет BOT_TOKEN" in complaints[0]["message"]


async def test_process_starts_as_soon_as_the_secret_appears() -> None:
    """Перепроверка — не украшение: секрет заводят после первого запуска."""
    missing = ["BOT_TOKEN"]
    started = asyncio.Event()

    async def run(_stop: asyncio.Event) -> None:
        started.set()

    service = Service(name="bot", requires=lambda _s: list(missing), run=run)
    stop = asyncio.Event()
    waiting = asyncio.create_task(serve(service, stop, recheck_s=FAST_RECHECK_S))

    await asyncio.sleep(FAST_RECHECK_S * 3)
    assert not started.is_set()
    missing.clear()
    await asyncio.wait_for(waiting, timeout=1)

    assert started.is_set()


async def test_configured_process_runs_at_once() -> None:
    seen: list[str] = []

    async def run(stop: asyncio.Event) -> None:
        seen.append("работаю")
        await stop.wait()

    stop = asyncio.Event()
    service = Service(name="worker", requires=lambda _s: [], run=run)
    waiting = asyncio.create_task(serve(service, stop))

    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(waiting, timeout=1)

    assert seen == ["работаю"]


async def test_idle_loop_reports_rarely_and_stops_on_signal() -> None:
    stop = asyncio.Event()
    ticks = 0

    async def tick() -> int:
        nonlocal ticks
        ticks += 1
        return 0

    with capture_logs() as logs:
        looping = asyncio.create_task(
            idle_loop(stop, tick, service="worker", poll_interval_s=0.001, idle_log_every_s=60)
        )
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(looping, timeout=1)

    assert ticks > 1, "цикл обязан крутиться, а не спать навсегда"
    idle = [entry for entry in logs if entry["event"] == "service.idle"]
    assert len(idle) == 1, "«нечего делать» не чаще раза в интервал, иначе лог бесполезен"


async def test_full_batch_is_followed_without_pause() -> None:
    """Пока работа есть, цикл не спит: иначе очередь разгребается по таймеру."""
    stop = asyncio.Event()
    handled = 0

    async def tick() -> int:
        nonlocal handled
        handled += 1
        if handled < 5:
            return 10
        stop.set()
        return 0

    # Пауза заведомо длиннее таймаута теста: если цикл уснёт после полной
    # пачки, тест не дождётся.
    await asyncio.wait_for(
        idle_loop(stop, tick, service="worker", poll_interval_s=30),
        timeout=1,
    )

    assert handled == 5


async def test_sleep_is_interrupted_by_stop() -> None:
    stop = asyncio.Event()

    assert await sleep_or_stop(stop, 0.001) is False
    stop.set()
    assert await sleep_or_stop(stop, 30) is True


@pytest.mark.parametrize(
    ("service", "settings", "expected"),
    [
        (bot_app.SERVICE, Settings(bot_token=""), ["BOT_TOKEN"]),
        (bot_app.SERVICE, Settings(bot_token="123:AA"), []),
        (notifier_main.SERVICE, Settings(bot_token=""), ["BOT_TOKEN"]),
        (notifier_main.SERVICE, Settings(bot_token="123:AA"), []),
        (
            collector_main.SERVICE,
            Settings(tg_api_id=0, tg_api_hash="", tg_session=""),
            ["TG_API_ID", "TG_API_HASH", "TG_SESSION"],
        ),
        (
            collector_main.SERVICE,
            Settings(tg_api_id=1, tg_api_hash="hash", tg_session=""),
            ["TG_SESSION"],
        ),
        (
            collector_main.SERVICE,
            Settings(tg_api_id=1, tg_api_hash="hash", tg_session="session"),
            [],
        ),
        # У воркера обязательных секретов нет: без базы он просто не найдёт
        # задач, и это не повод не запускаться.
        (worker_main.SERVICE, Settings(), []),
    ],
    ids=[
        "bot_no_token",
        "bot_ready",
        "notifier_no_token",
        "notifier_ready",
        "collector_empty",
        "collector_no_session",
        "collector_ready",
        "worker_always_ready",
    ],
)
def test_each_process_names_what_it_misses(
    service: Service,
    settings: Settings,
    expected: list[str],
) -> None:
    """Имена ровно те, что в `.env`: лог читает тот, кто этот `.env` и правит."""
    assert service.requires(settings) == expected


def test_every_compose_command_has_an_entry_point() -> None:
    """docker-compose запускает четыре модуля — все четыре обязаны существовать."""
    from importlib.util import find_spec

    for process in ("bot", "collector", "worker", "notifier"):
        assert find_spec(f"sniffer.{process}.__main__") is not None
