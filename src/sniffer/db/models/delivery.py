"""Подписки, дедуп доставки и очередь отправки."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB, REAL
from sqlalchemy.orm import Mapped, mapped_column

from sniffer.db.models.base import NOW, TRUE, ZERO, Base, BigIdMixin


class Subscription(BigIdMixin, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("user_id", "passport_root"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    passport_root: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=TRUE)
    mode: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'instant'"))
    max_per_day: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("5"))
    quiet_from: Mapped[time | None] = mapped_column(Time)
    quiet_to: Mapped[time | None] = mapped_column(Time)
    sent_today: Mapped[int] = mapped_column(Integer, nullable=False, server_default=ZERO)
    day_bucket: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class Notification(BigIdMixin, Base):
    """Дедуп доставки: одно объявление уходит в подписку ровно один раз."""

    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("subscription_id", "listing_id"),)

    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), nullable=False
    )
    score: Mapped[float] = mapped_column(REAL, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class Outbox(BigIdMixin, Base):
    __tablename__ = "outbox"
    __table_args__ = (Index("outbox_due_idx", "status", "scheduled_at"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=ZERO)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
