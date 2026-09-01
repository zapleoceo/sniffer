"""Уборка протухшего сырья по расписанию — «крон», живущий в процессе.

Почему не `crontab` на сервере. Состояние машины определяется репозиторием
(CLAUDE.md, «CI/CD»): строчка в чужом crontab не переживёт пересборку машины,
не попадёт ни в один диff и не будет видна тому, кто читает код. Здесь же
расписание — обычный код: его видно в ревью, оно едет тем же деплоем и
проверяется тестами без сервера.

Почему в воркере. Уборка воронки — работа воронки, а холостой цикл у него уже
есть. Отдельный седьмой контейнер ради одного `DELETE` раз в сутки на общей
машине не оправдан ничем.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import structlog

from sniffer.db.engine import session_scope
from sniffer.db.repositories.raw_messages import RawMessageRepository

log = structlog.get_logger(__name__)

# Сколько держим скачанное. Решение владельца от 01.09.2026: 90 дней вместо
# 30, которые стояли в комментарии схемы и никогда не исполнялись. Порог
# двухнедельной «живости» лота (verifier) к этому числу отношения не имеет:
# там речь о доверии к объявлению, здесь — о месте на диске.
RETENTION_DAYS = 90
# Раз в сутки. Чаще незачем: за сутки протухает столько же, сколько за сутки.
SWEEP_EVERY_S = 24 * 60 * 60
# Пачка на один проход. Первая уборка накопленного за месяцы одним запросом
# держала бы длинную транзакцию; пачками цикл сам продолжит без паузы.
BATCH = 1000

Now = Callable[[], datetime]
Monotonic = Callable[[], float]
Sweep = Callable[[datetime, int], Awaitable[int]]


async def sweep_once(older_than: datetime, limit: int) -> int:
    """Одна пачка удаления. Единственное место, где уборка касается базы."""
    async with session_scope() as session:
        deleted = await RawMessageRepository(session).delete_expired(
            older_than=older_than, limit=limit
        )
        await session.commit()
        return deleted


class Retention:
    """Расписание уборки. Состояние — только «когда следующий заход».

    Отметка держится в памяти процесса намеренно. Перезапуск сдвинет её на
    сутки вперёд, и это не потеря: уборка идемпотентна, а лишний прогон стоит
    одного запроса, который ничего не найдёт. Колонка в БД ради этого была бы
    схемой под удобство планировщика, а не под предметную область.
    """

    def __init__(
        self,
        *,
        days: int = RETENTION_DAYS,
        every_s: float = SWEEP_EVERY_S,
        batch: int = BATCH,
        sweep: Sweep = sweep_once,
        now: Now = lambda: datetime.now(UTC),
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        self._days = days
        self._every_s = every_s
        self._batch = batch
        self._sweep = sweep
        self._now = now
        self._monotonic = monotonic
        # Первый заход — сразу после старта: если процесс лежал неделю, ждать
        # ещё сутки не за чем.
        self._due_at = monotonic()

    async def tick(self) -> int:
        """Сколько строк убрали за проход. Ноль — либо не срок, либо чисто."""
        if self._monotonic() < self._due_at:
            return 0

        older_than = self._now() - timedelta(days=self._days)
        deleted = await self._sweep(older_than, self._batch)
        if deleted:
            log.info("retention.swept", deleted=deleted, older_than=older_than.isoformat())
        if deleted >= self._batch:
            # Пачка полная — протухшего осталось ещё. Следующий проход цикла
            # идёт без паузы, а срок не сдвигаем.
            return deleted

        self._due_at = self._monotonic() + self._every_s
        return deleted
