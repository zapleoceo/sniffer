"""ORM mirror of the additive collection queue migration."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sniffer.db.models.base import NOW, TRUE, ZERO, Base, BigIdMixin


class CollectionTask(BigIdMixin, Base):
    __tablename__ = "collection_tasks"

    dedup_key: Mapped[str] = mapped_column(Text, unique=True)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=ZERO)
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=text("3"))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    lease_token: Mapped[str | None] = mapped_column(Text)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class CollectionSubscriber(Base):
    __tablename__ = "collection_subscribers"

    task_id: Mapped[int] = mapped_column(
        ForeignKey("collection_tasks.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, server_default=TRUE)


class CollectionAction(Base):
    __tablename__ = "collection_actions"

    task_id: Mapped[int] = mapped_column(
        ForeignKey("collection_tasks.id", ondelete="CASCADE"), primary_key=True
    )
    action_key: Mapped[str] = mapped_column(Text, primary_key=True)
    arguments_hash: Mapped[str] = mapped_column(Text)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
