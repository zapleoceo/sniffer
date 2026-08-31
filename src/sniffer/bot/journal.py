"""Журнал диалога: что спросили, что ответили, сколько это заняло.

Живёт рядом с ботом, а не в `db/`: границу единицы работы ставит вызывающий
(architecture.md, 5.1), и решение «здесь транзакция закрылась» — это про
обработку сообщения, а не про SQL.

Главное свойство: журнал **не смеет мешать клиенту**. Недоступная база означает
пустой лог в дашборде, а не «бот не ответил». Поэтому каждый метод возвращает
`None` при ошибке и пишет предупреждение в лог, а вызывающий продолжает.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import structlog

from sniffer.db.engine import session_scope
from sniffer.db.repositories.dialog import DialogRepository
from sniffer.db.repositories.requests import ClientRequestRepository
from sniffer.db.repositories.users import UserRepository

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class Stopwatch:
    """Замер по этапам: `intake_ms`, `plan_ms`, `search_ms`.

    Нужен именно по этапам, а не одной цифрой: «запрос шёл 40 секунд» не
    отвечает на вопрос, разбор это или источники, а от ответа зависит, что
    оптимизировать.
    """

    stages: dict[str, int] = field(default_factory=dict)
    _mark: float = field(default_factory=time.monotonic)

    def lap(self, stage: str) -> None:
        now = time.monotonic()
        self.stages[stage] = int((now - self._mark) * 1000)
        self._mark = now


@dataclass(slots=True, frozen=True)
class OpenRequest:
    """Открытый запрос клиента: наш id пользователя и id запроса."""

    user_id: int
    request_id: int


async def open_request(
    tg_user_id: int, text: str, *, username: str | None = None
) -> OpenRequest | None:
    """Завести клиента (если новый) и открыть запрос. Ошибка — не блокер."""
    try:
        async with session_scope() as session:
            user = await UserRepository(session).get_or_create(tg_user_id, username=username)
            if user.id is None:  # pragma: no cover — id выдан вставкой
                raise LookupError("у клиента нет id после вставки")
            request = await ClientRequestRepository(session).open(user.id, text)
            await DialogRepository(session).log_incoming(user.id, text, request_id=request.id)
            await session.commit()
            return OpenRequest(user_id=user.id, request_id=request.id)
    # Широкий except намеренно: журнал не смеет ронять диалог.
    except Exception as exc:
        log.warning("journal.open_failed", kind=type(exc).__name__, error=str(exc))
        return None


async def log_answer(opened: OpenRequest | None, text: str) -> None:
    """Записать то, что клиент увидел."""
    if opened is None:
        return
    try:
        async with session_scope() as session:
            await DialogRepository(session).log_outgoing(
                opened.user_id, text, request_id=opened.request_id
            )
            await session.commit()
    # Широкий except намеренно: журнал не смеет ронять диалог.
    except Exception as exc:
        log.warning("journal.answer_failed", kind=type(exc).__name__, error=str(exc))


async def close_request(
    opened: OpenRequest | None,
    *,
    stages: dict[str, int],
    result_count: int = 0,
    plan_fallback: bool = False,
    sources: list[str] | None = None,
    error: str | None = None,
) -> None:
    """Закрыть запрос: длительность, этапы, результат или причина отказа."""
    if opened is None:
        return
    try:
        async with session_scope() as session:
            await ClientRequestRepository(session).finish(
                opened.request_id,
                result_count=result_count,
                stages=stages,
                plan_fallback=plan_fallback,
                sources=sources,
                error=error,
            )
            await session.commit()
    # Широкий except намеренно: журнал не смеет ронять диалог.
    except Exception as exc:
        log.warning("journal.close_failed", kind=type(exc).__name__, error=str(exc))
