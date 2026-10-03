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
from datetime import datetime, timedelta
from typing import Protocol

from sniffer.config import get_settings
from sniffer.db.engine import session_scope
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.db.repositories.tabs import TabRepository
from sniffer.db.repositories.users import UserRepository
from sniffer.domain.records import OutboxMessage


class Queue(Protocol):
    """Очередь `outbox`: то, что нотифаер делает со строками."""

    async def take_pending(
        self, *, limit: int, now: datetime | None = None
    ) -> list[OutboxMessage]: ...

    async def lock_pending(self, ids: Sequence[int], *, now: datetime) -> list[OutboxMessage]: ...

    async def mark_sent(self, message_id: int, *, now: datetime | None = None) -> None: ...

    async def mark_failed(
        self, message_id: int, *, retry_at: datetime, error: str | None = None
    ) -> None: ...

    async def give_up(self, message_id: int, *, error: str | None = None) -> None: ...

    async def cancel_pending_of(self, user_id: int, *, reason: str) -> int: ...

    async def cancel_for_blocked_users(self, *, reason: str) -> int: ...

    async def cancel_expired(self, *, now: datetime, ttl: timedelta) -> int: ...


class Tabs(Protocol):
    """Темы Telegram: куда класть сообщение поиска. Нет объекта — нет тем, как до них."""

    async def threads_for(self, subscription_ids: Sequence[int]) -> dict[int, int]: ...

    async def mark_lost(self, subscription_id: int) -> bool: ...


class Users(Protocol):
    """Клиенты: нотифаеру нужна одна запись — «писать этому клиенту нельзя»."""

    async def set_bot_blocked(
        self, tg_user_id: int, *, blocked: bool, at: datetime
    ) -> int | None: ...


@dataclass(frozen=True, slots=True)
class Work:
    """Одна короткая транзакция: репозитории и её собственный `commit`."""

    queue: Queue
    users: Users
    commit: Callable[[], Awaitable[None]]
    tabs: Tabs | None = None


Scope = Callable[[], AbstractAsyncContextManager[Work]]


@asynccontextmanager
async def work_scope() -> AsyncIterator[Work]:
    """Боевая единица работы: своя сессия, коммит — за вызывающим."""
    async with session_scope() as session:
        yield Work(
            queue=DeliveryRepository(session),
            users=UserRepository(session),
            commit=session.commit,
            # Без флага тем нотифаер шлёт, как слал: связей он не читает вовсе.
            tabs=TabRepository(session) if get_settings().topics_enabled else None,
        )
