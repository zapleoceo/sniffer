"""Проводка агента слежения: две задачи воркера, предохранитель, сводка «ещё N» в тексте."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from structlog.testing import capture_logs

from sniffer.domain.monitoring import OVERFLOW_KIND
from sniffer.notifier.delivery import render
from sniffer.worker import __main__ as worker_main


class Recorder:
    """Подмена `idle_loop`: запоминает, какие циклы запущены, и сразу возвращается."""

    def __init__(self) -> None:
        self.loops: dict[str, tuple[Callable[[], Awaitable[int]], float | None]] = {}

    async def __call__(
        self,
        stop: asyncio.Event,
        tick: Callable[[], Awaitable[int]],
        *,
        service: str,
        poll_interval_s: float | None = None,
    ) -> None:
        self.loops[service] = (tick, poll_interval_s)


class FakeAgent:
    def __init__(self, result: int = 3) -> None:
        self.result = result
        self.calls = 0

    async def tick(self) -> int:
        self.calls += 1
        return self.result


def _blank(monkeypatch: pytest.MonkeyPatch, agent: FakeAgent, recorder: Recorder) -> None:
    for name in (
        "Retention",
        "ArchivePipeline",
        "ChototSync",
        "Expiry",
        "Recategorize",
        "Screening",
        "ReservationSweep",
    ):
        monkeypatch.setattr(worker_main, name, lambda: object())
    monkeypatch.setattr(worker_main, "build_monitor", lambda: agent)
    monkeypatch.setattr(worker_main, "idle_loop", recorder)


async def test_the_worker_runs_the_pipeline_and_the_monitor_as_two_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, recorder = FakeAgent(), Recorder()
    _blank(monkeypatch, agent, recorder)

    await worker_main.run(asyncio.Event())

    assert set(recorder.loops) == {worker_main.NAME, "monitor"}
    tick, poll = recorder.loops["monitor"]
    assert poll == worker_main.MONITOR_POLL_S, (
        "агент опрашивает базу секундами, а не циклом воронки"
    )
    assert await tick() == 3 and agent.calls == 1


async def test_a_failing_monitor_pass_is_logged_and_does_not_stop_the_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_main, "MONITOR_RETRY_S", 0.0)

    async def broken() -> int:
        raise RuntimeError("база недоступна")

    with capture_logs() as logs:
        assert await worker_main.guarded(broken, name="monitor") == 0

    [entry] = [item for item in logs if item["event"] == "worker.task_failed"]
    assert entry["task"] == "monitor" and "RuntimeError" in entry["error"]


@pytest.mark.parametrize("stop", [asyncio.CancelledError, KeyboardInterrupt])
async def test_a_stop_signal_is_not_swallowed_by_the_guard(stop: type[BaseException]) -> None:
    async def stopped() -> int:
        raise stop()

    with pytest.raises(stop):
        await worker_main.guarded(stopped, name="monitor")


def test_the_pipeline_tick_no_longer_runs_the_monitor() -> None:
    import inspect

    names = list(inspect.signature(worker_main._tick).parameters)
    assert "matcher" not in names and "monitor" not in names


def test_the_overflow_summary_names_the_count_and_the_cap() -> None:
    text = render({"kind": OVERFLOW_KIND, "count": 7, "cap": 10})
    assert "7" in text and "10" in text
    assert "Сузьте запрос" in text


@pytest.mark.parametrize("junk", [None, "семь", 3.5, True, [], {}])
def test_a_garbage_summary_payload_does_not_break_the_render(junk: Any) -> None:
    # `render` не вправе бросать на чужих данных: одна плохая строка не роняет проход нотифаера,
    # а мусор не просачивается в текст клиенту — вместо числа он видит ноль.
    text = render({"kind": OVERFLOW_KIND, "count": junk, "cap": junk})
    assert "ещё 0 сверх 0" in text
