"""MCP returns market facts, not original message text or arbitrary metadata."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from sniffer.domain.passport import Category, Currency, PricePeriod

Deal = Literal["sell", "rent_out", "wanted"]
Source = Literal["telegram", "chotot"]
Cursor = Annotated[int, Field(gt=0, le=2**63 - 1)]
Amount = Annotated[int, Field(ge=0, le=10**12)]
PageSize = Annotated[int, Field(ge=1, le=50)]
Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
ModelName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_ -]{0,39}$")]
Count = Annotated[int, Field(ge=0, le=30)]


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city: Slug | None = None
    district: Slug | None = None
    category: Category | None = None
    deal_type: Deal | None = None
    source: Source | None = None
    min_price_vnd: Amount | None = None
    max_price_vnd: Amount | None = None
    price_period: PricePeriod | None = None
    model: ModelName | None = None
    min_rooms: Count | None = None
    furnished: bool | None = None
    balcony: bool | None = None
    separate_kitchen: bool | None = None


class PublicListing(BaseModel):
    id: int
    category: Category
    city: str
    district: str | None
    deal_type: Deal
    price_amount: Decimal | None
    price_currency: Currency | None
    price_period: PricePeriod | None
    source: Source
    url: str
    posted_at: datetime
    rooms: int | None = None
    area_m2: float | None = None
    furnished: bool | None = None
    balcony: bool | None = None
    separate_kitchen: bool | None = None
    model: str | None = None
    engine_cc: int | None = None
    year: int | None = None


class Page(BaseModel):
    items: list[PublicListing]
    next_before_id: int | None


class Location(BaseModel):
    slug: str
    name: str
    city: str


class CatalogGuide(BaseModel):
    cities: dict[str, str]
    districts: list[Location]
    models: list[str]
    categories: list[Category]
    deal_types: list[Deal]
    sources: list[Source]
