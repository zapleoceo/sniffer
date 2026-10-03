"""Карантин сбойной подписки: пауза растёт, причина и счётчик остаются в базе.

Зачем он нужен. Один «плохой» паспорт (незнакомое значение перечисления после отката кода,
оборванная запись) прежде валил весь проход, а с ним и воркер: Docker перезапускал процесс,
цикл повторялся, и вся воронка стояла из-за одной подписки (D5 разведки R5). Теперь
подписка изолируется, остальные обслуживаются, а больная возвращается в обход сама, когда
истечёт пауза: чинить её руками ради возвращения не нужно.

Пауза растёт вдвое с каждым сбоем подряд — 5, 10, 20 минут … — и упирается в потолок. Первая
короткая: чаще всего сбой разовый (оборванное соединение), и подписка не должна молчать час
из-за секунды сети. Потолок нужен, чтобы по-настоящему больная подписка не нагружала проход
чаще раза в несколько часов, но и не выпадала навсегда.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

import structlog

from sniffer.db.repositories.monitors import MonitorRepository

log = structlog.get_logger(__name__)

QUARANTINE_FIRST = timedelta(minutes=5)
QUARANTINE_MAX = timedelta(hours=6)
# Предел показателя степени, а не числа сбоев: `timedelta * 2**1000` — переполнение.
_MAX_DOUBLINGS = 16


def quarantine_delay(streak: int) -> timedelta:
    """Пауза после `streak`-го сбоя подряд."""
    doublings = min(max(streak - 1, 0), _MAX_DOUBLINGS)
    return min(QUARANTINE_FIRST * (1 << doublings), QUARANTINE_MAX)


class Slot(Protocol):
    """Всё, что карантину нужно знать о подписке; и разобранная, и больная её имеют."""

    @property
    def id(self) -> int: ...

    @property
    def user_id(self) -> int: ...

    @property
    def failed_streak(self) -> int: ...


async def quarantine(
    monitors: MonitorRepository,
    slot: Slot,
    *,
    error: str,
    moment: datetime,
    cause: BaseException | None = None,
) -> None:
    """Изолировать подписку: записать причину и счётчик, назначить время возвращения."""
    streak = slot.failed_streak + 1
    until = moment + quarantine_delay(streak)
    await monitors.quarantine(slot.id, now=moment, streak=streak, until=until, error=error)
    log.error(
        "monitor.quarantined",
        subscription=slot.id,
        user=slot.user_id,
        streak=streak,
        until=until.isoformat(),
        error=error,
        exc_info=cause,
    )
