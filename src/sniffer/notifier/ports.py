"""Что нотифаеру нужно от базы и где у него кончается транзакция.

Граница транзакции отдана нотифаеру явно, потому что именно он решает, когда
коммитить: на КАЖДОЕ сообщение, а не на пачку. Пока коммит стоял в конце прохода,
`docker compose stop` (десять секунд до SIGKILL) убивал процесс посреди пачки из
двадцати сообщений с паузой в секунду между ними, и уже отправленные не были
помечены — после рестарта они уходили второй раз.

Протоколы узкие, а не репозитории целиком: `Delivery` собирается в тесте без
Postgres, а то, что подставляют, обязано иметь те же сигнатуры — это проверяют и
mypy (структурно), и тест на совпадение подписей с настоящим репозиторием.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sniffer.db.engine import session_scope
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.domain.records import OutboxMessage


class Queue(Protocol):
    """Очередь `outbox`: то, что нотифаер делает со строками."""

    async def take_pending(
        self, *, limit: int, now: datetime | None = None
    ) -> list[OutboxMessage]: ...

    async def lock_pending(self, ids: Sequence[int], *, now: datetime) -> list[OutboxMessage]: ...

    async def mark_sent(self, message_id: int, *, now: datetime | None = None) -> None: ...

    async def mark_failed(self, message_id: int, *, retry_at: datetime) -> None: ...

    async def give_up(self, message_id: int) -> None: ...


@dataclass(frozen=True, slots=True)
class Work:
    """Одна короткая транзакция: репозитории и её собственный `commit`."""

    queue: Queue
    commit: Callable[[], Awaitable[None]]


Scope = Callable[[], AbstractAsyncContextManager[Work]]


@asynccontextmanager
async def work_scope() -> AsyncIterator[Work]:
    """Боевая единица работы: своя сессия, коммит — за вызывающим."""
    async with session_scope() as session:
        yield Work(queue=DeliveryRepository(session), commit=session.commit)
