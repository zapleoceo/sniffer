"""Общие заготовки тестов прохода догона: карточка и строка «карточка + текст»."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sniffer.domain.listing_patch import ListingWithText
from sniffer.domain.prices import PriceFact
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


def fact(
    amount: int,
    *,
    period: str | None = None,
    currency: str = "VND",
    source: str = "label",
    up_to: int | None = None,
) -> PriceFact:
    """Найденная в тексте цена — как её отдал бы разбор, но заданная руками."""
    return PriceFact("текст цены", amount, currency, period, source, up_to)


class Parser:
    """Разбор текста, подменённый заглушкой: отдаёт заданный факт и помнит вопросы.

    Политика замены проверяется на заданных фактах, а не на живом разборе: правки
    разбора её тесты не ломают.
    """

    def __init__(self, found: PriceFact | None) -> None:
        self.found = found
        self.asked: list[tuple[str | None, str | None]] = []

    def __call__(
        self, text: str, *, category: str | None = None, deal_type: str | None = None
    ) -> PriceFact | None:
        self.asked.append((category, deal_type))
        return self.found
