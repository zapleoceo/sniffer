"""Источники: сообщества, авторы объявлений и сырьё как пришло."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
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


class Chat(BigIdMixin, Base):
    __tablename__ = "chats"

    tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    username: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(Text, nullable=False)
    categories: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=EMPTY_ARRAY
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=TRUE)
    search_rank: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("100"))
    msg_count_24h: Mapped[int] = mapped_column(Integer, nullable=False, server_default=ZERO)
    last_msg_id: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=ZERO)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class Seller(BigIdMixin, Base):
    __tablename__ = "sellers"

    tg_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(Text)
    posts_30d: Mapped[int] = mapped_column(Integer, nullable=False, server_default=ZERO)
    distinct_chats: Mapped[int] = mapped_column(Integer, nullable=False, server_default=ZERO)
    scam_score: Mapped[float] = mapped_column(REAL, nullable=False, server_default=ZERO)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class RawMessage(BigIdMixin, Base):
    __tablename__ = "raw_messages"
    __table_args__ = (
        UniqueConstraint("chat_tg_id", "msg_id"),
        Index("raw_messages_stage_idx", "stage", sa_text("posted_at DESC")),
        Index("raw_messages_hash_idx", "text_hash"),
        Index("raw_messages_posted_idx", "posted_at"),
    )

    chat_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    msg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    seller_id: Mapped[int | None] = mapped_column(ForeignKey("sellers.id", ondelete="SET NULL"))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(Text, nullable=False)
    has_media: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=FALSE)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    stage: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'pending'"))
    gate_signals: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=EMPTY_JSON
    )
