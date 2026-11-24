"""Read-only SQL for the public catalog. Never select raw messages or seller data."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from decimal import Decimal
from re import fullmatch
from typing import Any, cast

from sqlalchemy import Integer, Row, Select, and_, case, func, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sniffer.db.models import Chat, Listing, RawMessage
from sniffer.db.repositories.base import Repository
from sniffer.domain.districts import PLACE_BY_SLUG
from sniffer.domain.passport import Category, Currency, PricePeriod
from sniffer.domain.public_catalog import Deal, Filters, Page, PublicListing

TELEGRAM = r"^https://t[.]me/[A-Za-z][A-Za-z0-9_]{3,31}/[1-9][0-9]{0,18}$"
CHOTOT = r"^https://(www[.])?chotot[.]com/[1-9][0-9]{0,18}[.]htm$"
# Only controlled taxonomy is returned; title, summary, seller, raw text, message media,
# custom attributes and arbitrary strings must not enter the public projection.
_DEALS = ("sell", "rent_out", "wanted")
_BOOL_FIELDS = ("furnished", "balcony")
_NUMBER_FIELDS = ("rooms", "area_m2", "engine_cc", "year")


def _public_rows(cities: Collection[str]) -> Select[Any]:
    origin = aliased(Chat)
    # Compare against the collector's CURRENT registry. A t.me username alone is not
    # proof of provenance. For forwarded posts both the source and original must qualify.
    local_link = (
        "https://t.me/"
        + func.ltrim(Chat.username, "@")
        + "/"
        + sql_cast(RawMessage.msg_id, Listing.tg_link.type)
    )
    forward_link = (
        "https://t.me/"
        + func.ltrim(origin.username, "@")
        + "/"
        + sql_cast(RawMessage.origin_msg_id, Listing.tg_link.type)
    )
    public_tg = and_(
        Listing.source.in_(("telegram_archive", "archive")),
        Chat.excluded_at.is_(None),
        Chat.is_active.is_(True),
        Chat.username.is_not(None),
        Listing.tg_link.op("~")(TELEGRAM),
        or_(
            and_(RawMessage.origin_kind.is_(None), Listing.tg_link == local_link),
            and_(
                RawMessage.origin_kind == "forward",
                origin.excluded_at.is_(None),
                origin.is_active.is_(True),
                Listing.tg_link == forward_link,
            ),
        ),
    )
    public_chotot = and_(
        Listing.source == "chotot",
        Listing.raw_message_id.is_(None),
        Listing.tg_link.op("~")(CHOTOT),
        or_(
            Listing.tg_link == ("https://www.chotot.com/" + Listing.external_id + ".htm"),
            Listing.tg_link == ("https://chotot.com/" + Listing.external_id + ".htm"),
        ),
    )
    fields = tuple(
        Listing.attributes[key].astext.label(key)
        for key in (*_BOOL_FIELDS, *_NUMBER_FIELDS, "model", "kitchen")
    )
    # The verified archive projection uses the legacy raw record as provenance, not its
    # persisted model summary. Match the original key in the exact format the projector uses.
    legacy = aliased(Listing)
    archive_id = (
        select(legacy.raw_message_id)
        .where(
            legacy.source == "telegram_archive",
            legacy.external_id == Listing.external_id,
        )
        .correlate(Listing)
        .scalar_subquery()
    )
    raw_id = case((Listing.source == "archive", archive_id), else_=Listing.raw_message_id)
    active_copy = (
        select(legacy.id)
        .where(
            legacy.source == "telegram_archive",
            legacy.external_id == Listing.external_id,
            legacy.is_active.is_(True),
            legacy.city == Listing.city,
            legacy.tg_link == Listing.tg_link,
        )
        .correlate(Listing)
        .exists()
    )
    return (
        select(
            Listing.id,
            Listing.source,
            RawMessage.origin_kind,
            Listing.category,
            Listing.city,
            Listing.district,
            Listing.deal_type,
            Listing.price_amount,
            Listing.price_currency,
            Listing.price_period,
            Listing.tg_link,
            Listing.posted_at,
            *fields,
        )
        .select_from(Listing)
        .outerjoin(RawMessage, raw_id == RawMessage.id)
        .outerjoin(Chat, RawMessage.chat_tg_id == Chat.tg_id)
        .outerjoin(origin, RawMessage.origin_chat_tg_id == origin.tg_id)
        .where(
            Listing.is_active.is_(True),
            Listing.city.in_(sorted(cities)),
            Listing.category.in_([item.value for item in Category]),
            Listing.deal_type.in_(_DEALS),
            or_(Listing.source != "archive", ~active_copy),
            or_(public_tg, public_chotot),
        )
    )


def _match(query: Select[Any], spec: Filters, models: Collection[str]) -> Select[Any]:
    for column, value in (
        (Listing.city, spec.city),
        (Listing.district, spec.district),
        (Listing.category, spec.category),
        (Listing.deal_type, spec.deal_type),
        (Listing.price_period, spec.price_period),
    ):
        if value is not None:
            query = query.where(column == value)
    if spec.district is not None:
        location = PLACE_BY_SLUG.get(spec.district)
        if location is None:
            raise ValueError("Unknown district; use catalog_guide.")
        query = query.where(Listing.city == location.city)
    if spec.source is not None:
        query = query.where(
            Listing.source.in_(
                ("telegram_archive", "archive") if spec.source == "telegram" else ("chotot",)
            )
        )
    if spec.source_reference_only:
        query = query.where(or_(Listing.source == "chotot", RawMessage.origin_kind == "forward"))
    low, high = spec.min_price_vnd, spec.max_price_vnd
    if low is not None and high is not None and low > high:
        raise ValueError("min_price_vnd exceeds max_price_vnd")
    if low is not None:
        query = query.where(Listing.price_currency == Currency.VND, Listing.price_amount >= low)
    if high is not None:
        query = query.where(Listing.price_currency == Currency.VND, Listing.price_amount <= high)
    if spec.model is not None:
        model = spec.model.strip().replace("-", "_").replace(" ", "_")
        if model not in models:
            raise ValueError("Unknown catalog model slug; omit the filter to browse.")
        query = query.where(Listing.attributes["model"].astext == model)
    if spec.min_rooms is not None:
        raw = Listing.attributes["rooms"].astext
        rooms = case((raw.op("~")(r"^[0-9]{1,2}$"), sql_cast(raw, Integer)), else_=None)
        query = query.where(rooms >= spec.min_rooms, rooms <= 30)
    for key in _BOOL_FIELDS:
        flag = getattr(spec, key)
        if flag is not None:
            query = query.where(Listing.attributes[key].astext == str(flag).lower())
    if spec.separate_kitchen is not None:
        states = ("separate",) if spec.separate_kitchen else ("shared",)
        query = query.where(Listing.attributes["kitchen"].astext.in_(states))
    return query


def _number(value: str | None, low: int, high: int) -> int | None:
    if value and fullmatch(r"[0-9]{1,5}", value) and low <= int(value) <= high:
        return int(value)
    return None


def _area(value: str | None) -> float | None:
    if value and fullmatch(r"[0-9]{1,5}(?:[.][0-9]{1,2})?", value) and 0 < float(value) <= 10000:
        return float(value)
    return None


def _boolean(value: str | None) -> bool | None:
    return {"true": True, "false": False}.get(value or "")


def _card(row: Row[Any], models: Collection[str]) -> PublicListing:
    district = PLACE_BY_SLUG.get(row.district)
    currency = row.price_currency if row.price_currency in set(Currency) else None
    price = cast(Decimal | None, row.price_amount) if currency else None
    return PublicListing(
        id=row.id,
        source="chotot" if row.source == "chotot" else "telegram",
        source_evidence=(
            "marketplace_post"
            if row.source == "chotot"
            else "telegram_forward_origin"
            if row.origin_kind == "forward"
            else "telegram_observed"
        ),
        category=Category(row.category),
        city=row.city,
        district=district.slug if district and district.city == row.city else None,
        deal_type=cast(Deal, row.deal_type),
        price_amount=price,
        price_currency=currency,
        price_period=row.price_period if row.price_period in set(PricePeriod) else None,
        url=row.tg_link,
        posted_at=cast(datetime, row.posted_at),
        rooms=_number(row.rooms, 0, 30),
        area_m2=_area(row.area_m2),
        model=row.model if row.model in models else None,
        engine_cc=_number(row.engine_cc, 1, 10000),
        year=_number(row.year, 1950, 2100),
        furnished=_boolean(row.furnished),
        balcony=_boolean(row.balcony),
        separate_kitchen={"separate": True, "shared": False}.get(row.kitchen),
    )


class PublicCatalogRepository(Repository):
    def __init__(
        self, session: AsyncSession, *, cities: Collection[str], models: Collection[str]
    ) -> None:
        super().__init__(session)
        self._cities = cities
        self._models = models

    async def page(self, spec: Filters, *, before_id: int | None, limit: int) -> Page:
        query = _match(_public_rows(self._cities), spec, self._models)
        if before_id is not None:
            query = query.where(Listing.id < before_id)
        rows = (
            await self._session.execute(query.order_by(Listing.id.desc()).limit(limit + 1))
        ).all()
        items = [_card(row, self._models) for row in rows[:limit]]
        return Page(items=items, next_before_id=items[-1].id if len(rows) > limit else None)

    async def detail(self, listing_id: int) -> PublicListing | None:
        result = await self._session.execute(
            _public_rows(self._cities).where(Listing.id == listing_id)
        )
        row = result.one_or_none()
        return _card(row, self._models) if row is not None else None
