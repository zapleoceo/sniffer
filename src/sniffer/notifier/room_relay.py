"""Серверная доставка кандидатов слежения в комнату агентов (vera-room).

Заменяет опрос из сессии: агент `dot` получает варианты жилья сообщением `room_post` и
проверяет их глазами. Живёт тиком в процессе нотифаера, без отдельного контейнера.

Курсор (`room_relay_cursor`) двигается ТОЛЬКО после того, как комната приняла сообщение, и
по одному: упавший пост не пропускается и не блокируется ретраем в цикле. Первая же ошибка
обрывает проход, следующий тик (через `POLL_INTERVAL_S`) начнёт с того же уведомления.
`message_id` детерминирован (`housing-cand-<notification_id>`) и совпадает с нумерацией
опроса из сессии, так что сервер комнаты дедуплицирует и двойной доставки нет.

Выключена полностью, пока не заданы URL, токен и подписки: ни одного HTTP-запроса.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.config import Settings
from sniffer.db.engine import session_scope
from sniffer.db.repositories.room_relay import RoomRelayRepository
from sniffer.domain.room_relay import RelayCandidate
from sniffer.notifier.room_body import build_body
from sniffer.notifier.room_client import RoomClient

log = structlog.get_logger(__name__)

# Порция за тик: комната не должна получить сотню сообщений разом после долгого простоя.
BATCH = 5
MESSAGE_ID_PREFIX = "housing-cand-"

Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class Room(Protocol):
    async def post(
        self, *, to: str, task_id: str, message_id: str, body: str, status: str = "request"
    ) -> None: ...


Connect = Callable[[], AbstractAsyncContextManager[Room]]


@dataclass(frozen=True, slots=True)
class RelayConfig:
    url: str
    token: str
    subscriptions: tuple[int, ...]
    to: str
    task_id: str

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token and self.subscriptions)

    @classmethod
    def from_settings(cls, settings: Settings) -> RelayConfig:
        ids: list[int] = []
        for part in settings.room_relay_subscriptions.split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
            elif part:
                log.warning("room_relay.bad_subscription_id", value=part[:20])
        return cls(
            url=settings.room_mcp_url.strip(),
            token=settings.room_token.strip(),
            subscriptions=tuple(ids),
            to=settings.room_relay_to.strip() or "dot",
            task_id=settings.room_relay_task_id.strip() or "sniffer-housing-watch",
        )


def message_id(item: RelayCandidate) -> str:
    return f"{MESSAGE_ID_PREFIX}{item.notification_id}"


class RoomRelay:
    def __init__(
        self,
        config: RelayConfig,
        *,
        connect: Connect | None = None,
        sessions: Sessions = session_scope,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._connect = connect or self._default_connect
        self._sessions = sessions
        self._now = now

    def _default_connect(self) -> AbstractAsyncContextManager[Room]:
        return RoomClient(self._config.url, self._config.token)

    def announce(self) -> None:
        """Одно событие при старте процесса: включена доставка или нет."""
        if self._config.enabled:
            log.info(
                "room_relay.enabled",
                subscriptions=list(self._config.subscriptions),
                to=self._config.to,
                task_id=self._config.task_id,
            )
        else:
            log.info("room_relay.disabled")

    async def tick(self) -> int:
        """Сколько кандидатов принято комнатой за проход. Ошибка не роняет нотифаер."""
        if not self._config.enabled:
            return 0
        try:
            return await self._deliver()
        except Exception:
            # Ждём следующего тика: курсор не сдвинут, повтор безопасен (dedup по message_id).
            log.exception("room_relay.failed")
            return 0

    async def _deliver(self) -> int:
        async with self._sessions() as session:
            pending = await RoomRelayRepository(session).pending(self._config.subscriptions, BATCH)
        if not pending:
            return 0
        delivered = 0
        async with self._connect() as room:
            for item in pending:
                await room.post(
                    to=self._config.to,
                    task_id=self._config.task_id,
                    message_id=message_id(item),
                    body=build_body(item, self._now()),
                )
                await self._advance(item)
                delivered += 1
                log.info("room_relay.delivered", notification_id=item.notification_id)
        return delivered

    async def _advance(self, item: RelayCandidate) -> None:
        async with self._sessions() as session:
            await RoomRelayRepository(session).advance(item.subscription_id, item.notification_id)
            await session.commit()
