"""Внутренняя очередь воронки.

Отдельной таблицей, а не брокером: разбирается через `SELECT … FOR UPDATE
SKIP LOCKED`, и на этом объёме Redis или Rabbit добавляют операционную
сложность без единой решаемой проблемы (architecture.md, раздел 3).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, Text
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sniffer.db.models.base import EMPTY_JSON, NOW, ZERO, Base, BigIdMixin


class Job(BigIdMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("jobs_due_idx", "status", "run_after", "id"),)

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=EMPTY_JSON
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=ZERO)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
