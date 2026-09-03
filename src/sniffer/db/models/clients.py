"""Клиенты бота и паспорта запроса."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, REAL
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


class User(BigIdMixin, Base):
    __tablename__ = "users"

    tg_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    username: Mapped[str | None] = mapped_column(Text)
    lang: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'ru'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    is_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=FALSE)
    # Явный указатель нужен при нескольких цепочках: «последний изменённый»
    # запрос не обязательно тот, с которым человек работает сейчас.
    active_passport_root: Mapped[int | None] = mapped_column(BigInteger)
    # Нажатие «изменить» переживает рестарт бота и следующий апдейт Telegram.
    editing_passport_root: Mapped[int | None] = mapped_column(BigInteger)


class Passport(BigIdMixin, Base):
    """Версия паспорта. `root_id` пуст у первой — корнем ей служит свой id."""

    __tablename__ = "passports"
    __table_args__ = (
        Index("passports_user_idx", "user_id", "is_current"),
        Index("passports_root_idx", "root_id", sa_text("version DESC")),
        # Ключ цепочки — COALESCE(root_id, id): у первой версии root_id пуст.
        # Уникальность здесь — барьер против двух одновременных уточнений: ни
        # двух версий с одним номером, ни двух актуальных в одной цепочке.
        Index(
            "passports_chain_version_idx",
            sa_text("(COALESCE(root_id, id))"),
            "version",
            unique=True,
        ),
        Index(
            "passports_chain_current_idx",
            sa_text("(COALESCE(root_id, id))"),
            unique=True,
            postgresql_where=sa_text("is_current"),
        ),
    )

    root_id: Mapped[int | None] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("1"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'draft'"))
    intent: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    districts: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=EMPTY_ARRAY
    )
    budget: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=EMPTY_JSON)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=EMPTY_JSON
    )
    must_have: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=EMPTY_ARRAY
    )
    deal_breakers: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=EMPTY_ARRAY
    )
    timeframe_from: Mapped[date | None] = mapped_column(Date)
    timeframe_to: Mapped[date | None] = mapped_column(Date)
    raw_query: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(REAL, nullable=False, server_default=ZERO)
    missing_fields: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=EMPTY_ARRAY
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=TRUE)


class PassportEvent(BigIdMixin, Base):
    __tablename__ = "passport_events"
    __table_args__ = (
        # Список видов закрыт: из этого лога собирается состояние диалога, и
        # опечатка в виде события даёт не ошибку, а молча потерянный вопрос.
        CheckConstraint(
            "kind IN ('user_message', 'feedback', 'agent_infer', 'manual_edit', 'question_asked')",
            name="passport_events_kind_check",
        ),
    )

    passport_id: Mapped[int] = mapped_column(
        ForeignKey("passports.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=EMPTY_JSON
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
