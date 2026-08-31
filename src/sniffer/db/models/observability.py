"""Наблюдаемость и сессии: то, что показывает веб-интерфейс владельца.

Раздел DDL `── наблюдаемость и сессии ──`. Четыре таблицы, и каждая существует
из-за конкретного вопроса, на который иначе не ответить: «кто и о чём спросил»,
«что человек увидел», «сколько это стоило», «жива ли сессия юзербота».
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, Text
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sniffer.db.models.base import (
    EMPTY_ARRAY,
    EMPTY_JSON,
    FALSE,
    NOW,
    TRUE,
    ZERO,
    Base,
    BigIdMixin,
)


class ClientRequest(BigIdMixin, Base):
    """Один запрос клиента — единица, к которой привязываются расходы и время."""

    __tablename__ = "client_requests"
    __table_args__ = (
        Index("client_requests_user_idx", "user_id", sa_text("started_at DESC")),
        Index("client_requests_recent_idx", sa_text("started_at DESC")),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    passport_id: Mapped[int | None] = mapped_column(ForeignKey("passports.id", ondelete="SET NULL"))
    raw_query: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'running'"))
    stages: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSON)
    plan_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=FALSE)
    sources: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=EMPTY_ARRAY
    )
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=ZERO)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class DialogMessage(BigIdMixin, Base):
    """Реплика переписки: то, что человек написал или увидел."""

    __tablename__ = "dialog_messages"
    __table_args__ = (Index("dialog_messages_user_idx", "user_id", sa_text("created_at DESC")),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    request_id: Mapped[int | None] = mapped_column(
        ForeignKey("client_requests.id", ondelete="SET NULL")
    )
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class BrokerCall(BigIdMixin, Base):
    """Один вызов LLM через AIbroker с токенами и стоимостью."""

    __tablename__ = "broker_calls"
    __table_args__ = (Index("broker_calls_request_idx", "request_id"),)

    request_id: Mapped[int | None] = mapped_column(
        ForeignKey("client_requests.id", ondelete="SET NULL")
    )
    broker_request_id: Mapped[int | None] = mapped_column(BigInteger)
    capability: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, server_default=ZERO)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, server_default=ZERO)
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=sa_text("0")
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class TelegramSession(BigIdMixin, Base):
    """Сессия юзербота. `session_enc` — Fernet-шифртекст, а не строка сессии."""

    __tablename__ = "telegram_sessions"

    phone: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    session_enc: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=TRUE)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
