"""Independent progress for future news processing of retained originals."""

from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, ForeignKey, Table, Text
from sqlalchemy import text as sa_text

from sniffer.db.models.base import Base

news_processing = Table(
    "news_processing",
    Base.metadata,
    Column(
        "raw_message_id",
        BigInteger,
        ForeignKey("raw_messages.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column("status", Text, nullable=False, server_default=sa_text("'pending'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    CheckConstraint("status IN ('pending', 'reviewed', 'rejected')"),
)
