"""Периоды квоты и журнал показанных карточек.

ORM-зеркало `infra/sql/010_quota_ledger.sql`. Формулу границ периода (CHECK)
здесь не повторяем: её единственная копия в базе, а модель существует ради
запросов и порядка очистки таблиц.

Составные внешние ключи описаны здесь же: без них `Base.metadata.sorted_tables`
не знал бы, что журнал зависит от периода, а период — от пары «клиент, якорь», и
очистка тестовой базы удаляла бы таблицы не в том порядке.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Mapped, mapped_column

from sniffer.db.models.base import NOW, Base, BigIdMixin


class QuotaPeriod(BigIdMixin, Base):
    """Один период квоты аккаунта: `[period_start, period_end)`, номер `k` от якоря."""

    __tablename__ = "quota_periods"
    __table_args__ = (
        UniqueConstraint("user_id", "period_no"),
        UniqueConstraint("id", "user_id"),
        # Якорь после появления периодов неизменяем: этот ключ не даст его поменять.
        ForeignKeyConstraint(["user_id", "anchor_at"], ["users.id", "users.quota_anchor_at"]),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    anchor_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_no: Mapped[int] = mapped_column(Integer, nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class OfferView(BigIdMixin, Base):
    """Показанная карточка: одна на пару «период, карточка». Пустой `delivered_at` — резерв."""

    __tablename__ = "offer_views"
    __table_args__ = (
        UniqueConstraint("period_id", "listing_id"),
        ForeignKeyConstraint(
            ["period_id", "user_id"],
            ["quota_periods.id", "quota_periods.user_id"],
            ondelete="CASCADE",
        ),
        Index("offer_views_user_listing_idx", "user_id", "listing_id"),
        Index(
            "offer_views_unconfirmed_idx",
            "shown_at",
            postgresql_where=sa_text("delivered_at IS NULL"),
        ),
    )

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    period_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Без CASCADE: карточки не удаляются, а квота не должна «возвращаться» молча.
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), nullable=False)
    passport_root: Mapped[int | None] = mapped_column(
        ForeignKey("passports.id", ondelete="SET NULL")
    )
    request_id: Mapped[int | None] = mapped_column(
        ForeignKey("client_requests.id", ondelete="SET NULL")
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    times_shown: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("1"))
    shown_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    last_shown_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
