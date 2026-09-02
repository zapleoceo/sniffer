"""Предложения: карточка и её фотографии."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB, REAL, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from sniffer.db.models.base import (
    EMBEDDING_DIM,
    EMPTY_JSON,
    NOW,
    TRUE,
    ZERO,
    Base,
    BigIdMixin,
)


class Listing(BigIdMixin, Base):
    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("raw_message_id"),
        UniqueConstraint("source", "external_id"),
        # Порядок колонок не декоративный: в этом же порядке ходит и разовый
        # подбор, и проверка подписок.
        Index(
            "listings_match_idx",
            "city",
            "category",
            "deal_type",
            "is_active",
            sa_text("posted_at DESC"),
        ),
        Index("listings_price_idx", "price_usd_month"),
        Index("listings_tsv_idx", "search_tsv", postgresql_using="gin"),
        Index(
            "listings_trgm_idx",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        Index("listings_attrs_idx", "attributes", postgresql_using="gin"),
    )

    raw_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("raw_messages.id", ondelete="CASCADE")
    )
    source: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sa_text("'telegram_archive'")
    )
    external_id: Mapped[str | None] = mapped_column(Text)
    seller_id: Mapped[int | None] = mapped_column(ForeignKey("sellers.id", ondelete="SET NULL"))
    deal_type: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(Text, nullable=False)
    district: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    price_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    price_currency: Mapped[str | None] = mapped_column(Text)
    price_period: Mapped[str | None] = mapped_column(Text)
    price_usd_month: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=EMPTY_JSON
    )
    tg_link: Mapped[str] = mapped_column(Text, nullable=False)
    lang: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(REAL, nullable=False, server_default=ZERO)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=TRUE)
    search_tsv: Mapped[str | None] = mapped_column(TSVECTOR)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))


class ListingMedia(BigIdMixin, Base):
    __tablename__ = "listing_media"
    __table_args__ = (UniqueConstraint("listing_id", "r2_key"),)

    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), nullable=False
    )
    r2_key: Mapped[str] = mapped_column(Text, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    bytes: Mapped[int | None] = mapped_column(Integer)
