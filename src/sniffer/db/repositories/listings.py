"""Карточки предложений."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import Integer, Select, Table, and_, case, func, or_, select, update
from sqlalchemy import cast as sql_cast
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
        """Свежая страница собственного каталога для разового поиска.

        Свойства паспорта отбираются здесь, а не только после `LIMIT`: иначе
        Lead трёхнедельной давности не попадал бы в сотню свежайших мотобайков
        и для клиента не существовал бы. Дисциплина та же, что у отбора выдачи
        (`search/relevance.py`): известное свойство обязано совпасть,
        неизвестное — не мешает, модель требует положительного совпадения.
        """
        statement = select(models.Listing).where(
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
            statement = statement.where(
                or_(
                    models.Listing.price_amount.is_(None),
                    models.Listing.price_amount <= spec.max_price_vnd,
                )
            )
        statement = _with_attributes(statement, spec)
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

    async def expire(self, *, older_than: datetime, limit: int) -> int:
        """Погасить пачку карточек, опубликованных раньше `older_than`."""
        stale = (
            select(models.Listing.id)
            .where(models.Listing.is_active.is_(True), models.Listing.posted_at < older_than)
            .limit(limit)
            .scalar_subquery()
        )
        result = await self._session.execute(
            update(models.Listing).where(models.Listing.id.in_(stale)).values(is_active=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def retire_unseen(self, source: str, *, city: str, category: str, seen: set[str]) -> int:
        """Погасить карточки источника в городе и категории, которых нет в `seen`."""
        statement = update(models.Listing).where(
            models.Listing.source == source,
            models.Listing.city == city,
            models.Listing.category == category,
            models.Listing.is_active.is_(True),
        )
        if seen:
            statement = statement.where(models.Listing.external_id.not_in(sorted(seen)))
        result = await self._session.execute(statement.values(is_active=False))
        return int(getattr(result, "rowcount", 0) or 0)

    async def live_archive_refs(
        self, chat_tg_id: int, *, since: datetime, limit: int
    ) -> list[tuple[int, int]]:
        """Активные карточки чата: (id карточки, id сообщения) для перечитывания."""
        prefix = f"{chat_tg_id}:"
        rows = await self._session.execute(
            select(models.Listing.id, models.Listing.external_id)
            .where(
                models.Listing.source == "telegram_archive",
                models.Listing.is_active.is_(True),
                models.Listing.posted_at >= since,
                models.Listing.external_id.startswith(prefix),
            )
            .order_by(models.Listing.posted_at.desc())
            .limit(limit)
        )
        refs: list[tuple[int, int]] = []
        for listing_id, external_id in rows:
            tail = str(external_id).removeprefix(prefix)
            if tail.isdigit():
                refs.append((int(listing_id), int(tail)))
        return refs

    async def deactivate_many(self, listing_ids: list[int]) -> int:
        if not listing_ids:
            return 0
        result = await self._session.execute(
            update(models.Listing)
            .where(models.Listing.id.in_(listing_ids), models.Listing.is_active.is_(True))
            .values(is_active=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def deactivate(self, listing_id: int) -> None:
        await self._session.execute(
            update(models.Listing).where(models.Listing.id == listing_id).values(is_active=False)
        )


def _with_attributes(statement: Select[Any], spec: MatchFilter) -> Select[Any]:
    """Свойства паспорта → условия по JSONB `attributes` и тексту карточки.

    «Известное ≠ несовпадение»: карточка без извлечённого свойства остаётся —
    половина объявлений марку не пишет, и выбросить их значит опустошить
    выдачу; карточка с ЯВНО другим значением уходит. Объём хранится числом в
    строке JSON, поэтому сравнивается через `CASE`: нечисловое значение — это
    «неизвестно», а не ошибка приведения на весь запрос.
    """
    attrs = models.Listing.attributes
    for key, value in spec.attributes.items():
        if value in (None, ""):
            continue
        statement = statement.where(or_(~attrs.has_key(key), attrs[key].astext == str(value)))
    if spec.model:
        # Модель — самый узкий критерий: её имя обязано быть в карточке (слаг в
        # атрибутах либо слова в тексте, слитно и раздельно — «air blade» и
        # «airblade» одно имя). Окончательно судит `rank_items` тем же
        # знанием о написаниях, что читает запрос клиента.
        phrase = spec.model.replace("_", " ").casefold()
        haystack = func.lower(models.Listing.title + " " + models.Listing.summary)
        statement = statement.where(
            or_(
                attrs["model"].astext == spec.model,
                haystack.contains(phrase),
                haystack.contains(phrase.replace(" ", "")),
            )
        )
    if spec.engine_cc_min is not None or spec.engine_cc_max is not None:
        numeric = attrs["engine_cc"].astext
        known = case((numeric.op("~")("^[0-9]+$"), sql_cast(numeric, Integer)), else_=None)
        bounds = []
        if spec.engine_cc_min is not None:
            bounds.append(known >= spec.engine_cc_min)
        if spec.engine_cc_max is not None:
            bounds.append(known <= spec.engine_cc_max)
        # Обе границы вместе (И): «200 кубиков» — это 150…250, а не «≥150
        # ИЛИ ≤250», то есть всё подряд. Неизвестный объём не мешает.
        statement = statement.where(or_(known.is_(None), and_(*bounds)))
    return statement
