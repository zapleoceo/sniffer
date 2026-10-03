"""Расписание снятия зависших резервов. Без базы: проверяется «когда», не «что».

Что именно снимается (только неподтверждённое, только диалог, только старше
срока) — в контрактных сценариях `test_quota_ledger.py` на обеих реализациях
журнала и на живом Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sniffer.domain.quota import RESERVATION_TTL
from sniffer.worker.quota_sweep import ReservationSweep

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


class Ticks:
    """Монотонные часы под управлением теста."""

    def __init__(self) -> None:
        self.value = 500.0

    def __call__(self) -> float:
        return self.value


def sweeper(
    ticks: Ticks, removed: list[int], cutoffs: list[datetime] | None = None, **kwargs: object
) -> ReservationSweep:
    async def sweep(older_than: datetime, _limit: int) -> int:
        if cutoffs is not None:
            cutoffs.append(older_than)
        return removed.pop(0) if removed else 0

    return ReservationSweep(sweep=sweep, now=lambda: T0, monotonic=ticks, **kwargs)  # type: ignore[arg-type]


async def test_the_first_sweep_happens_at_once() -> None:
    """Процесс мог лежать: зависшее при падении снимается без лишней минуты."""
    assert await sweeper(Ticks(), [4]).tick() == 4


async def test_a_swept_minute_is_not_swept_again_until_the_next_one() -> None:
    ticks = Ticks()
    keeper = sweeper(ticks, [2, 1], every_s=60)

    assert await keeper.tick() == 2
    ticks.value += 30
    assert await keeper.tick() == 0, "раз в минуту, а не раз в проход цикла"
    ticks.value += 30
    assert await keeper.tick() == 1


async def test_a_full_batch_goes_on_without_waiting() -> None:
    ticks = Ticks()
    keeper = sweeper(ticks, [100, 100, 7], batch=100, every_s=60)

    assert await keeper.tick() == 100
    assert await keeper.tick() == 100, "полная пачка — зависших осталось ещё"
    assert await keeper.tick() == 7, "неполная пачка закрывает минуту"
    assert await keeper.tick() == 0


async def test_the_cutoff_is_the_reservation_lifetime_back_from_now() -> None:
    cutoffs: list[datetime] = []

    await sweeper(Ticks(), [], cutoffs).tick()

    assert cutoffs == [T0 - RESERVATION_TTL]
    assert RESERVATION_TTL == timedelta(minutes=10)


class Step:
    """Любой шаг воронки: делает «сколько-то работы» и помнит, что его позвали."""

    def __init__(self, work: int) -> None:
        self.work = work
        self.called = 0

    async def tick(self) -> int:
        self.called += 1
        return self.work


async def test_the_worker_loop_runs_the_sweep_and_counts_its_work() -> None:
    """Забытый вызов или потерянное слагаемое молча оставили бы слоты занятыми."""
    from sniffer.worker import __main__ as worker

    steps = {name: Step(index) for index, name in enumerate(["a", "b", "c", "d", "e", "f", "g"], 1)}
    sweep = Step(1000)

    tick: Any = worker._tick  # шаги — подставные, сигнатура их не знает
    total = await tick(*steps.values(), sweep)

    assert sweep.called == 1
    assert total == sum(step.work for step in steps.values()) + 1000, "работа уборки не учтена"
