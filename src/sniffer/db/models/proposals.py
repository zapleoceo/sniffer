"""Proposal table mirror, intentionally no DDL execution or automatic approval."""

from sqlalchemy import BigInteger, Column, DateTime, String, Table
from sqlalchemy.dialects.postgresql import JSONB

from sniffer.db.models.base import Base

proposals = Table(
    "schema_proposals",
    Base.metadata,
    Column("id", BigInteger, primary_key=True),
    Column("owner_kind", String, nullable=False),
    Column("owner_id", BigInteger, nullable=False),
    Column("content_hash", String, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("status", String, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
