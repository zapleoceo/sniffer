"""Core table mirrors for independently verified catalog observations."""

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, String, Table, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from sniffer.db.models.base import Base

observations = Table(
    "catalog_observations",
    Base.metadata,
    Column("id", BigInteger, primary_key=True),
    Column("task_id", BigInteger, ForeignKey("collection_tasks.id"), nullable=False),
    Column("source", String, nullable=False),
    Column("external_id", String, nullable=False),
    Column("content_hash", String, nullable=False),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
publications = Table(
    "catalog_publications",
    Base.metadata,
    Column("source", String, primary_key=True),
    Column("external_id", String, primary_key=True),
    Column("observation_id", BigInteger, ForeignKey("catalog_observations.id"), nullable=False),
    Column("city", String, nullable=False),
    Column("category", String, nullable=False),
    Column("deal_type", String, nullable=False),
    Column("price_vnd", BigInteger),
    Column("active", Boolean, nullable=False),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    Column("verified_at", DateTime(timezone=True), nullable=False),
    # Ключи личности и метка дубля (026_primary_sources.sql).
    Column("identity_keys", ARRAY(Text), nullable=False, server_default="{}"),
    Column("duplicate_of_source", String),
    Column("duplicate_of_external_id", String),
)
coverage = Table(
    "catalog_coverage",
    Base.metadata,
    Column("scope_key", String, primary_key=True),
    Column("source", String, primary_key=True),
    Column("task_id", BigInteger, ForeignKey("collection_tasks.id"), nullable=False),
    Column("checked_at", DateTime(timezone=True), nullable=False),
    Column("outcome", String, nullable=False),
)
