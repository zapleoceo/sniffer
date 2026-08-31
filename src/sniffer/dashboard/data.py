"""Чтение данных для страниц. Только через репозитории, без единой строки SQL.

Всё в этом модуле — свои данные. В брокер за расходами не ходим: страница,
зависящая от доступности чужого сервиса, покажет пустоту ровно тогда, когда на
неё смотрят из-за инцидента (docs/dashboard.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sniffer.db.engine import session_scope
from sniffer.db.repositories.broker_calls import BrokerCallRepository
from sniffer.db.repositories.dialog import DialogRepository
from sniffer.db.repositories.requests import ClientRequestRepository
from sniffer.db.repositories.stats import StatsRepository
from sniffer.db.repositories.telegram_sessions import TelegramSessionRepository
from sniffer.db.repositories.users import UserRepository
from sniffer.domain.records import (
    BrokerCall,
    ClientRequest,
    DialogMessage,
    SessionState,
    User,
)

# Сколько строк лога показываем на обзоре. Больше на одну страницу не нужно, а
# «показать всё» на растущей таблице — это способ уронить страницу через год.
RECENT_REQUESTS = 50
DIALOG_TAIL = 60


@dataclass(slots=True)
class RequestRow:
    """Строка лога запросов вместе со своими расходами."""

    request: ClientRequest
    tokens: int = 0
    cost_usd: Decimal = Decimal(0)


@dataclass(slots=True)
class Overview:
    stats: dict[str, int] = field(default_factory=dict)
    request_totals: dict[str, int] = field(default_factory=dict)
    cost_totals: dict[str, object] = field(default_factory=dict)
    users: list[User] = field(default_factory=list)
    requests_by_user: dict[int, int] = field(default_factory=dict)
    requests: list[RequestRow] = field(default_factory=list)
    session: SessionState | None = None


@dataclass(slots=True)
class RequestDetail:
    row: RequestRow
    user: User | None
    dialog: list[DialogMessage] = field(default_factory=list)
    calls: list[BrokerCall] = field(default_factory=list)


@dataclass(slots=True)
class UserDetail:
    user: User
    dialog: list[DialogMessage] = field(default_factory=list)


async def overview() -> Overview:
    """Всё, что показывает главная. Одна сессия — один снимок состояния."""
    async with session_scope() as session:
        requests = await ClientRequestRepository(session).recent(limit=RECENT_REQUESTS)
        costs = await BrokerCallRepository(session).cost_by_request(
            [request.id for request in requests]
        )
        return Overview(
            stats=await StatsRepository(session).summary(),
            request_totals=await ClientRequestRepository(session).totals(),
            cost_totals=await BrokerCallRepository(session).totals(),
            users=await UserRepository(session).recent(),
            requests_by_user=await ClientRequestRepository(session).counts_by_user(),
            requests=[_row(request, costs) for request in requests],
            session=await TelegramSessionRepository(session).active_state(),
        )


async def request_detail(request_id: int) -> RequestDetail | None:
    async with session_scope() as session:
        request = await ClientRequestRepository(session).get(request_id)
        if request is None:
            return None
        costs = await BrokerCallRepository(session).cost_by_request([request_id])
        return RequestDetail(
            row=_row(request, costs),
            user=await UserRepository(session).get(request.user_id),
            dialog=await DialogRepository(session).by_request(request_id),
            calls=await BrokerCallRepository(session).by_request(request_id),
        )


async def user_detail(user_id: int) -> UserDetail | None:
    async with session_scope() as session:
        user = await UserRepository(session).get(user_id)
        if user is None:
            return None
        return UserDetail(
            user=user,
            dialog=await DialogRepository(session).by_user(user_id, limit=DIALOG_TAIL),
        )


async def session_states() -> list[SessionState]:
    async with session_scope() as session:
        return await TelegramSessionRepository(session).states()


async def save_session(phone: str, session_string: str) -> None:
    async with session_scope() as session:
        await TelegramSessionRepository(session).save(phone, session_string)
        await session.commit()


def _row(request: ClientRequest, costs: dict[int, tuple[int, Decimal]]) -> RequestRow:
    tokens, cost = costs.get(request.id, (0, Decimal(0)))
    return RequestRow(request=request, tokens=tokens, cost_usd=cost)
