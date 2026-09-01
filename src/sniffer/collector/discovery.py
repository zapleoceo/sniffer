"""Боевой запуск очереди Telegram-групп из единственного коллектора.

Разведка не должна жить только в тестах: этот модуль связывает её строгие
протоколы с репозиториями и запускает ровно один безопасный проход. Сетевое
подключение открывается только на время прохода — текущий живой поиск всё ещё
использует тот же MTProto-аккаунт из бот-процесса, поэтому постоянное второе
соединение было бы ненужным риском дублирования auth key.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.collector.alerts import session_unavailable
from sniffer.collector.history_store import DatabaseHistoryStore
from sniffer.collector.ingest import HistorySyncer
from sniffer.config import Settings, get_settings
from sniffer.db.engine import session_scope
from sniffer.db.repositories.chats import ChatRepository
from sniffer.db.repositories.discovery import (
    CandidateRepository,
    JoinLedgerRepository,
    RejectRepository,
)
from sniffer.domain.records import Chat, DiscoveryCandidate
from sniffer.sources.telegram_discover import ChatDiscovery
from sniffer.sources.telegram_discover_client import new_joiner
from sniffer.sources.telegram_discover_joiner import ChatJoiner
from sniffer.sources.telegram_discover_reference import (
    JOIN_WINDOW,
    MAX_JOINS_PER_DAY,
    ChatCandidate,
    DiscoveredChat,
    JoinState,
    TelegramJoiner,
)

log = structlog.get_logger(__name__)


@asynccontextmanager
async def _session() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


class DatabaseRegistry:
    async def has_chat(self, *, tg_id: int | None = None, username: str = "") -> bool:
        async with _session() as session:
            return await ChatRepository(session).has_identity(tg_id=tg_id, username=username)

    async def count(self) -> int:
        async with _session() as session:
            return await ChatRepository(session).count()

    async def add(self, chat: DiscoveredChat) -> None:
        async with _session() as session:
            await ChatRepository(session).add(
                Chat(
                    tg_id=chat.tg_id,
                    username=chat.username or None,
                    title=chat.title,
                    city=chat.city,
                    search_rank=chat.search_rank,
                )
            )


class DatabaseQueue:
    async def push(self, candidate: ChatCandidate) -> None:
        async with _session() as session:
            await CandidateRepository(session).push(_candidate(candidate))

    async def reserve(self) -> ChatCandidate | None:
        async with _session() as session:
            candidate = await CandidateRepository(session).reserve()
        return _source_candidate(candidate) if candidate is not None else None

    async def release(self, key: str) -> int:
        async with _session() as session:
            return await CandidateRepository(session).release(key)

    async def drop(self, key: str) -> None:
        async with _session() as session:
            await CandidateRepository(session).drop(key)

    async def is_queued(self, key: str) -> bool:
        async with _session() as session:
            return await CandidateRepository(session).contains(key)


class DatabaseRejected:
    async def is_rejected(self, key: str) -> bool:
        async with _session() as session:
            return await RejectRepository(session).contains(key)

    async def reject(self, key: str, reason: str) -> None:
        async with _session() as session:
            await RejectRepository(session).reject(key, reason)


class DatabaseLedger:
    async def state(self, now: datetime) -> JoinState:
        async with _session() as session:
            state = await JoinLedgerRepository(session).state(now, window=JOIN_WINDOW)
        return JoinState(state.joins_in_window, state.next_allowed_at, state.blocked_until)

    async def claim_slot(self, now: datetime, *, next_allowed_at: datetime) -> int | None:
        async with _session() as session:
            return await JoinLedgerRepository(session).claim_slot(
                now,
                window=JOIN_WINDOW,
                maximum=MAX_JOINS_PER_DAY,
                next_allowed_at=next_allowed_at,
            )

    async def confirm_join(self, *, event_id: int, tg_id: int, username: str) -> None:
        async with _session() as session:
            await JoinLedgerRepository(session).confirm_join(
                event_id, tg_id=tg_id, username=username
            )

    async def release_slot(self, *, event_id: int) -> None:
        async with _session() as session:
            await JoinLedgerRepository(session).release_slot(event_id)

    async def record_flood(self, *, event_id: int, blocked_until: datetime) -> None:
        async with _session() as session:
            await JoinLedgerRepository(session).record_flood(event_id, blocked_until=blocked_until)

    async def record_mute_failure(self, *, tg_id: int, error: str) -> None:
        async with _session() as session:
            await JoinLedgerRepository(session).record_mute_failure(tg_id=tg_id, error=error)

    async def mark_muted(self, *, tg_id: int) -> None:
        async with _session() as session:
            await JoinLedgerRepository(session).mark_muted(tg_id=tg_id)

    async def pending_mutes(self) -> list[int]:
        async with _session() as session:
            return await JoinLedgerRepository(session).pending_mutes()


class JoinerLike(Protocol):
    async def join_next(self) -> DiscoveredChat | None: ...

    async def retry_mutes(self) -> int: ...


class HistoryLike(Protocol):
    async def sync(self) -> int: ...


JoinerFactory = Callable[[TelegramJoiner], JoinerLike]
HistoryFactory = Callable[[TelegramJoiner], HistoryLike]
ClientFactory = Callable[[Settings], TelegramJoiner]
OwnerAlert = Callable[[Settings, str], Awaitable[None]]


class DiscoveryRunner:
    """Один проход: догнать mute и, если лимиты разрешают, один join."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client_factory: ClientFactory = new_joiner,
        joiner_factory: JoinerFactory | None = None,
        history_factory: HistoryFactory | None = None,
        owner_alert: OwnerAlert = session_unavailable,
    ) -> None:
        self._settings = settings or get_settings()
        self._client_factory = client_factory
        self._joiner_factory = joiner_factory or self._new_joiner
        self._history_factory = history_factory or self._new_history
        self._owner_alert = owner_alert
        self._unavailable_reported = False

    async def tick(self) -> int:
        client: TelegramJoiner | None = None
        try:
            client = self._client_factory(self._settings)
            await client.connect()
        except Exception as exc:
            # Не продолжаем тем же циклом «лечить» сессию сетью: следующая
            # попытка лишь через расписание коллектора, а причина видна в логе.
            error = type(exc).__name__
            log.error("collector.telegram_unavailable", error=error)
            if not self._unavailable_reported:
                self._unavailable_reported = True
                try:
                    await self._owner_alert(self._settings, error)
                except Exception as alert_error:
                    log.warning(
                        "collector.session_alert_failed",
                        error=f"{type(alert_error).__name__}: {alert_error}",
                    )
            if client is not None:
                await _disconnect_quietly(client)
            return 0
        self._unavailable_reported = False
        try:
            joiner = self._joiner_factory(client)
            muted = await joiner.retry_mutes()
            joined = await joiner.join_next()
            await self._history_factory(client).sync()
            if joined is None:
                return muted
            log.info("collector.chat_joined", tg_id=joined.tg_id, chat=joined.username)
            return muted + 1
        finally:
            await _disconnect_quietly(client)

    def _new_joiner(self, client: TelegramJoiner) -> ChatJoiner:
        return ChatJoiner(
            registry=DatabaseRegistry(),
            queue=DatabaseQueue(),
            ledger=DatabaseLedger(),
            rejected=DatabaseRejected(),
            client=client,
            city=self._settings.default_city,
        )

    def _new_history(self, client: TelegramJoiner) -> HistorySyncer:
        discovery = ChatDiscovery(
            registry=DatabaseRegistry(),
            queue=DatabaseQueue(),
            rejected=DatabaseRejected(),
            client=client,
            city=self._settings.default_city,
        )
        return HistorySyncer(
            reader=client,
            store=DatabaseHistoryStore(),
            discover=discovery.harvest,
        )


def _candidate(candidate: ChatCandidate) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        key=candidate.key,
        username=candidate.username,
        invite_hash=candidate.invite_hash,
        found_in=candidate.found_in,
        priority=candidate.priority,
    )


def _source_candidate(candidate: DiscoveryCandidate) -> ChatCandidate:
    return ChatCandidate(
        key=candidate.key,
        username=candidate.username,
        invite_hash=candidate.invite_hash,
        found_in=candidate.found_in,
        priority=candidate.priority,
    )


async def _disconnect_quietly(client: TelegramJoiner) -> None:
    try:
        await client.disconnect()
    except Exception as exc:
        log.warning("collector.telegram_disconnect_failed", error=f"{type(exc).__name__}: {exc}")
