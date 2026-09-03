"""Immutable source observations; a customer's passport is never evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sniffer.domain.passport import Category, PricePeriod

SOURCE_HOSTS = {
    "chotot": frozenset({"chotot.com", "www.chotot.com"}),
    "telegram_archive": frozenset({"t.me"}),
    "telegram_groups": frozenset({"t.me"}),
    "archive": frozenset({"t.me"}),
}


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    field: Literal["city", "category", "deal_type", "price_vnd", "price_period", "active"]
    quote: str = Field(min_length=1, max_length=2000)


class CatalogFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    city: str | None = Field(default=None, min_length=1, max_length=100)
    category: Category | None = None
    deal_type: Literal["sell", "rent_out", "wanted"] | None = None
    price_vnd: int | None = Field(default=None, ge=0, le=10**15)
    price_period: PricePeriod | None = None
    active: bool | None = None


class CatalogObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
    external_id: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    fetched_at: datetime
    posted_at: datetime | None = None
    title: str = Field(min_length=1, max_length=500)
    raw_text: str = Field(min_length=1, max_length=30000)
    extractor_version: str = Field(min_length=1, max_length=100)
    facts: CatalogFacts
    evidence: tuple[Evidence, ...] = Field(max_length=6)

    @model_validator(mode="after")
    def grounded(self) -> Self:
        parsed = urlsplit(self.url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in SOURCE_HOSTS.get(self.source, frozenset())
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or not parsed.path.strip("/")
        ):
            raise ValueError("invalid_source_link")
        for stamp in (self.fetched_at, self.posted_at):
            if stamp is not None and (stamp.tzinfo is None or stamp.utcoffset() is None):
                raise ValueError("timezone_required")
        if self.posted_at is not None and self.posted_at > self.fetched_at:
            raise ValueError("posted_after_fetch")
        fields = {item.field for item in self.evidence}
        if len(fields) != len(self.evidence):
            raise ValueError("duplicate_evidence")
        for evidence in self.evidence:
            if evidence.quote not in self.raw_text or getattr(self.facts, evidence.field) is None:
                raise ValueError("ungrounded_evidence")
        known = {name for name, value in self.facts.model_dump().items() if value is not None}
        if known != fields:
            raise ValueError("every_fact_needs_evidence")
        return self

    @property
    def publishable(self) -> bool:
        """Unknown location/type/activity remain in staging, never inferred from scope."""
        facts = self.facts
        return all(
            value is not None
            for value in (facts.city, facts.category, facts.deal_type, facts.active)
        )
