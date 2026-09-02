"""Материализация проверенных live-находок в общий каталог."""

from __future__ import annotations

from datetime import UTC
from decimal import Decimal

import structlog

from sniffer.domain.passport import Passport, counterpart_deal_type
from sniffer.domain.records import Listing
from sniffer.sources.base import RawItem

log = structlog.get_logger(__name__)


async def remember(items: list[RawItem], passport: Passport) -> int:
    """Сохранить датированные находки; сбой кэша не ломает текущую выдачу."""
    if not passport.city or passport.category is None:
        return 0
    eligible = [item for item in items if item.posted_at is not None]
    if not eligible:
        return 0
    try:
        from sniffer.sources.chat_directory import store_listings

        return await store_listings([_listing(item, passport) for item in eligible])
    except Exception as exc:
        log.warning("catalog.remember_failed", error=f"{type(exc).__name__}: {exc}")
        return 0


def _listing(item: RawItem, passport: Passport) -> Listing:
    posted = item.posted_at
    assert posted is not None and passport.category is not None
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    return Listing(
        raw_message_id=None,
        source=item.source,
        external_id=item.external_id,
        deal_type=counterpart_deal_type(passport.intent) or "sell",
        category=passport.category.value,  # caller rejects an empty category
        city=passport.city or "",
        title=item.title or item.text[:180] or "объявление",
        summary=item.text or item.title,
        price_amount=Decimal(item.price_vnd) if item.price_vnd is not None else None,
        price_currency="VND" if item.price_vnd is not None else None,
        price_period=passport.budget.period.value,
        # Паспорт описывает желание клиента, а не доказанный факт объявления.
        # Явные свойства позже извлечёт pipeline; приписывать желаемое найденному
        # означало бы отравить будущий поиск ложными структурными данными.
        attributes={},
        tg_link=item.url,
        posted_at=posted,
        confidence=0.7,
    )
