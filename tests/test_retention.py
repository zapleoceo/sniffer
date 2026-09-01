"""Расписание уборки сырья. Без базы: проверяется только «когда», не «что».

Что именно удаляется — на живом Postgres, в `test_db_repositories.py`: там
условие про `ON DELETE CASCADE`, и на подделке оно зелёное всегда.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sniffer.worker.retention import Retention

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


class Clock:
    """Монотонные часы под управлением теста."""

    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def retention(clock: Clock, deleted: list[int], **kwargs: object) -> Retention:
    async def sweep(_older_than: datetime, _limit: int) -> int:
        return deleted.pop(0) if deleted else 0

    return Retention(
        sweep=sweep,
        now=lambda: NOW,
        monotonic=clock,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_the_first_sweep_happens_at_once_not_a_day_later() -> None:
    """Процесс мог лежать неделю — ждать ещё сутки не за чем."""
    clock = Clock()
    assert await retention(clock, [7]).tick() == 7


async def test_a_swept_day_is_not_swept_again_until_tomorrow() -> None:
    clock = Clock()
    keeper = retention(clock, [3], every_s=86400)

    assert await keeper.tick() == 3
    clock.value += 3600  # час спустя
    assert await keeper.tick() == 0, "уборка раз в сутки, а не раз в проход"
    clock.value += 86400
    assert await keeper.tick() == 0, "убирать больше нечего, но заход состоялся"


async def test_a_full_batch_keeps_going_without_waiting_a_day() -> None:
    """Первая уборка накопленного за месяцы не растягивается на месяцы.

    Полная пачка означает «протухшего осталось ещё»: срок не сдвигаем, и
    холостой цикл идёт следующим проходом без паузы.
    """
    clock = Clock()
    keeper = retention(clock, [100, 100, 42], batch=100, every_s=86400)

    assert await keeper.tick() == 100
    assert await keeper.tick() == 100, "полная пачка обязана продолжиться сразу"
    assert await keeper.tick() == 42, "неполная пачка закрывает сутки"
    assert await keeper.tick() == 0


async def test_the_cutoff_is_the_retention_window_back_from_now() -> None:
    """Порог считается от «сейчас», а не от старта процесса."""
    seen: list[datetime] = []

    async def sweep(older_than: datetime, _limit: int) -> int:
        seen.append(older_than)
        return 0

    clock = Clock()
    await Retention(days=90, sweep=sweep, now=lambda: NOW, monotonic=clock).tick()

    assert seen == [datetime(2026, 6, 3, 12, 0, tzinfo=UTC)]
