"""Карточки предложений."""

from __future__ import annotations

from sqlalchemy import select

from sniffer.db import models
from sniffer.db.mappers import to_listing
from sniffer.db.repositories.base import Repository
from sniffer.domain.records import Listing


class ListingRepository(Repository):
    async def add(self, listing: Listing) -> Listing:
        """Вставка карточки. Возвращает её же с проставленным `id`.

        Одно сырое сообщение даёт ровно одну карточку — это `UNIQUE
        (raw_message_id)` в схеме. Повторное извлечение по тому же сообщению
        упрётся в него, и это правильно: молча дублировать карточку хуже, чем
        упасть на повторе.
        """
        row = models.Listing(
            raw_message_id=listing.raw_message_id,
            seller_id=listing.seller_id,
            deal_type=listing.deal_type,
            category=listing.category,
            city=listing.city,
            district=listing.district,
            title=listing.title,
            summary=listing.summary,
            price_amount=listing.price_amount,
            price_currency=listing.price_currency,
            price_period=listing.price_period,
            price_usd_month=listing.price_usd_month,
            attributes=dict(listing.attributes),
            tg_link=listing.tg_link,
            lang=listing.lang,
            confidence=listing.confidence,
            posted_at=listing.posted_at,
            is_active=listing.is_active,
        )
        self._session.add(row)
        await self._session.flush()
        return to_listing(row)

    async def get(self, listing_id: int) -> Listing | None:
        row = await self._session.get(models.Listing, listing_id)
        return to_listing(row) if row is not None else None

    async def get_by_raw_message(self, raw_message_id: int) -> Listing | None:
        """Извлекали ли уже эту карточку — проверка перед повторным вызовом LLM."""
        row = await self._session.scalar(
            select(models.Listing).where(models.Listing.raw_message_id == raw_message_id)
        )
        return to_listing(row) if row is not None else None
