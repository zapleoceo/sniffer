"""Общие заготовки тестов прохода догона: карточка и строка «карточка + текст»."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sniffer.domain.listing_patch import ListingWithText
from sniffer.domain.records import Listing

POSTED = datetime(2026, 9, 1, tzinfo=UTC)


def card(
    listing_id: int = 41,
    *,
    category: str = "apartment",
    deal_type: str = "rent_out",
    price: int | None = None,
    attributes: dict[str, Any] | None = None,
    **changes: Any,
) -> Listing:
    """Карточка с ценой «как в базе»: донги и срок, согласованный со стороной сделки."""
    fields: dict[str, Any] = {
        "raw_message_id": listing_id + 1000,
        "deal_type": deal_type,
        "category": category,
        "city": "nha_trang",
        "title": f"Объявление {listing_id}",
        "summary": "сводка",
        "tg_link": f"https://t.me/c/1/{listing_id}",
        "posted_at": POSTED,
        "attributes": dict(attributes or {}),
        "id": listing_id,
    }
    if price is not None:
        fields.update(
            price_amount=Decimal(price),
            price_currency="VND",
            price_period="month" if deal_type == "rent_out" else "once",
        )
    return Listing(**{**fields, **changes})


def row(
    listing_id: int,
    text: str | None,
    *,
    category: str = "apartment",
    deal_type: str = "rent_out",
    price: int | None = None,
    attributes: dict[str, Any] | None = None,
) -> ListingWithText:
    return ListingWithText(
        card(
            listing_id, category=category, deal_type=deal_type, price=price, attributes=attributes
        ),
        text,
    )
