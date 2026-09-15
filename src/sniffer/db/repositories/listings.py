"""Карточки предложений."""

from __future__ import annotations

from typing import cast

from sqlalchemy import Table, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sniffer.db import models
from sniffer.db.mappers import to_listing
from sniffer.db.repositories.base import Repository
from sniffer.domain.records import Listing, MatchFilter


class ListingRepository(Repository):
    _REPLACEABLE_FIELDS = (
        "raw_message_id",
        "source",
        "external_id",
        "seller_id",
        "deal_type",
        "category",
        "city",
        "district",
        "title",
        "summary",
        "price_amount",
        "price_currency",
        "price_period",
        "price_usd_month",
        "attributes",
        "tg_link",
        "lang",
        "confidence",
        "posted_at",
        "is_active",
    )

    async def add(self, listing: Listing) -> Listing:
        """Вставка карточки. Возвращает её же с проставленным `id`.

        Одно сырое сообщение даёт ровно одну карточку — это `UNIQUE
        (raw_message_id)` в схеме. Повторное извлечение по тому же сообщению
        упрётся в него, и это правильно: молча дублировать карточку хуже, чем
        упасть на повторе.
        """
        row = models.Listing(
            raw_message_id=listing.raw_message_id,
            source=listing.source,
            external_id=listing.external_id,
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

    async def upsert_external(self, listing: Listing) -> bool:
        """Сохранить находку живого источника один раз."""
        if not listing.external_id:
            return False
        table = cast(Table, models.Listing.__table__)
        values = {
            "raw_message_id": None,
            "source": listing.source,
            "external_id": listing.external_id,
            "deal_type": listing.deal_type,
            "category": listing.category,
            "city": listing.city,
            "title": listing.title,
            "summary": listing.summary,
            "price_amount": listing.price_amount,
            "price_currency": listing.price_currency,
            "price_period": listing.price_period,
            "attributes": dict(listing.attributes),
            "tg_link": listing.tg_link,
            "lang": listing.lang,
            "confidence": listing.confidence,
            "posted_at": listing.posted_at,
            "is_active": listing.is_active,
        }
        result = await self._session.execute(
            pg_insert(table)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["source", "external_id"])
            .returning(table.c.id)
        )
        return result.scalar_one_or_none() is not None

    async def match(
        self, spec: MatchFilter, *, after_id: int = 0, limit: int = 50
    ) -> list[Listing]:
        """Карточки под условия подписки, начиная с `after_id`.

        Курсор по `id`, а не по времени: воркер идёт по подпискам и обязан
        двигаться вперёд ровно один раз по каждой карточке. По времени это не
        получается — две карточки одной секунды либо повторятся, либо
        потеряются, смотря какое сравнение выбрать.

        Индекс `listings_match_idx` покрывает `city, category, deal_type,
        is_active, posted_at DESC` — условия ниже подобраны под него.
        """
        statement = select(models.Listing).where(
            models.Listing.id > after_id,
            models.Listing.city == spec.city,
            models.Listing.is_active.is_(True),
        )
        if spec.category is not None:
            statement = statement.where(models.Listing.category == spec.category)
        if spec.deal_type is not None:
            statement = statement.where(models.Listing.deal_type == spec.deal_type)
        if spec.since is not None:
            statement = statement.where(models.Listing.posted_at >= spec.since)
        if spec.max_price_vnd is not None:
            # Карточку без цены не отбрасываем: минимальная карточка её ещё не
            # извлекает, и «цены нет» не значит «дорого». Решает потом score.
            statement = statement.where(
                or_(
                    models.Listing.price_amount.is_(None),
                    models.Listing.price_amount <= spec.max_price_vnd,
                )
            )
        rows = await self._session.scalars(statement.order_by(models.Listing.id).limit(limit))
        return [to_listing(row) for row in rows]

    async def search_catalog(self, spec: MatchFilter, *, limit: int = 100) -> list[Listing]:
        """Свежая страница собственного каталога для разового поиска."""
        statement = select(models.Listing).where(
            models.Listing.city == spec.city,
            models.Listing.is_active.is_(True),
        )
        if spec.category is not None:
            statement = statement.where(models.Listing.category == spec.category)
        if spec.deal_type is not None:
            statement = statement.where(models.Listing.deal_type == spec.deal_type)
        if spec.max_price_vnd is not None:
            statement = statement.where(
                or_(
                    models.Listing.price_amount.is_(None),
                    models.Listing.price_amount <= spec.max_price_vnd,
                )
            )
        rows = await self._session.scalars(
            statement.order_by(models.Listing.posted_at.desc(), models.Listing.id.desc()).limit(
                limit
            )
        )
        return [to_listing(row) for row in rows]

    async def max_id(self) -> int:
        """Верхняя граница курсора: докуда подписке имеет смысл догонять."""
        return int(await self._session.scalar(select(func.max(models.Listing.id))) or 0)

    async def get(self, listing_id: int) -> Listing | None:
        row = await self._session.get(models.Listing, listing_id)
        return to_listing(row) if row is not None else None

    async def get_by_raw_message(self, raw_message_id: int) -> Listing | None:
        """Извлекали ли уже эту карточку — проверка перед повторным вызовом LLM."""
        row = await self._session.scalar(
            select(models.Listing).where(models.Listing.raw_message_id == raw_message_id)
        )
        return to_listing(row) if row is not None else None

    async def get_by_fingerprint(self, text_hash: str, *, besides: int) -> Listing | None:
        """Return the active canonical card for another copy of the same advert."""
        row = await self._session.scalar(
            select(models.Listing)
            .join(models.RawMessage, models.Listing.raw_message_id == models.RawMessage.id)
            .where(
                models.RawMessage.text_hash == text_hash,
                models.RawMessage.id != besides,
                models.Listing.is_active.is_(True),
            )
            .order_by(models.Listing.posted_at.desc(), models.Listing.id.desc())
            .limit(1)
        )
        return to_listing(row) if row is not None else None

    async def refresh(self, listing_id: int, replacement: Listing) -> Listing:
        """Move a canonical duplicate card to its freshest original and facts."""
        values = {
            field: dict(value) if field == "attributes" else value
            for field in self._REPLACEABLE_FIELDS
            for value in (getattr(replacement, field),)
        }
        await self._session.execute(
            update(models.Listing).where(models.Listing.id == listing_id).values(**values)
        )
        await self._session.flush()
        refreshed = await self.get(listing_id)
        if refreshed is None:
            raise ValueError("listing_not_found")
        return refreshed

    async def deactivate(self, listing_id: int) -> None:
        await self._session.execute(
            update(models.Listing)
            .where(models.Listing.id == listing_id)
            .values(is_active=False)
        )
