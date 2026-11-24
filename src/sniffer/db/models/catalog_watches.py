"""SQLAlchemy metadata mirror of 027_catalog_watches.sql."""

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from sniffer.db.models.base import NOW, ZERO, Base

catalog_watch_credentials = Table(
    "catalog_watch_credentials",
    Base.metadata,
    Column("tg_chat_id", BigInteger, primary_key=True, autoincrement=False),
    Column("token_hash", CHAR(64), nullable=False, unique=True),
    Column("issued_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    CheckConstraint("tg_chat_id > 0"),
)

catalog_watches = Table(
    "catalog_watches",
    Base.metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column(
        "tg_chat_id",
        BigInteger,
        ForeignKey("catalog_watch_credentials.tg_chat_id"),
        nullable=False,
    ),
    Column("filters", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    Column("next_check_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    Column("after_id", BigInteger, nullable=False, server_default=ZERO),
    UniqueConstraint("tg_chat_id", "filters"),
    Index("catalog_watches_due_idx", "next_check_at", "id"),
)

catalog_watch_delivered = Table(
    "catalog_watch_delivered",
    Base.metadata,
    Column(
        "tg_chat_id",
        BigInteger,
        ForeignKey("catalog_watch_credentials.tg_chat_id"),
        primary_key=True,
    ),
    Column("canonical_url", Text, primary_key=True),
    Column("delivered_at", DateTime(timezone=True), nullable=False, server_default=NOW),
)
