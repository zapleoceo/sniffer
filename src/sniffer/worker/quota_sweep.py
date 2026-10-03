"""Снятие зависших резервов журнала показов — расписание внутри воркера.

Резерв записывается до отправки сообщения и подтверждается после. Процесс мог
умереть между этими двумя шагами (рестарт на деплое, OOM-killer общей машины), и
тогда человек платил бы карточкой, которой не видел: слот занят, показа не было.
Раз в минуту воркер удаляет резервы старше десяти минут — карточка остаётся
«непоказанной», слот возвращается.

Тот же приём, что у `Retention`, и по той же причине: уборка — работа воркера, и
отдельный контейнер ради одного `DELETE` раз в минуту на общей машине не
оправдан. Состояние — только «когда следующий заход», в памяти: рестарт сдвинет
его не больше чем на минуту, а уборка идемпотентна.

Снимаются только резервы диалога (`search`, `deferred`): подтверждение слежения
придёт от нотификатора, возможно через часы (тихие часы, дайджест), и снять его по
таймеру значило бы потерять запись о том, что карточка присылалась.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import structlog

from sniffer.db.engine import session_scope
from sniffer.db.repositories.quota import QuotaRepository
from sniffer.domain.quota import RESERVATION_TTL

log = structlog.get_logger(__name__)

# Раз в минуту: зависший резерв — это человек, у которого слот занят зря, а не
# отчётность, и ждать час незачем. Холостой проход стоит одного пустого запроса.
SWEEP_EVERY_S = 60.0
# Пачка на один проход. Накопиться много зависших могло только при долгой аварии.
BATCH = 500

Now = Callable[[], datetime]
Monotonic = Callable[[], float]
Sweep = Callable[[datetime, int], Awaitable[int]]


async def sweep_once(older_than: datetime, limit: int) -> int:
    """Одна пачка. Единственное место, где уборка резервов касается базы."""
    async with session_scope() as session:
        removed = await QuotaRepository(session).sweep_stale(older_than=older_than, limit=limit)
        await session.commit()
        return removed


class ReservationSweep:
    def __init__(
        self,
        *,
        ttl: timedelta = RESERVATION_TTL,
        every_s: float = SWEEP_EVERY_S,
        batch: int = BATCH,
        sweep: Sweep = sweep_once,
        now: Now = lambda: datetime.now(UTC),
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._every_s = every_s
        self._batch = batch
        self._sweep = sweep
        self._now = now
        self._monotonic = monotonic
        # Первый заход — сразу после старта: если процесс лежал, резервы, зависшие
        # при падении, снимаются без лишней минуты ожидания.
        self._due_at = monotonic()

    async def tick(self) -> int:
        """Сколько резервов сняли за проход. Ноль — либо не срок, либо чисто."""
        if self._monotonic() < self._due_at:
            return 0
        removed = await self._sweep(self._now() - self._ttl, self._batch)
        if removed:
            log.info("quota.reservations_swept", removed=removed)
        if removed >= self._batch:
            # Пачка полная — зависших осталось ещё: следующий проход без паузы.
            return removed
        self._due_at = self._monotonic() + self._every_s
        return removed
