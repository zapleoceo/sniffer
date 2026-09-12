"""Периодический обход Chotot в общий каталог `listings`.

До 12.09.2026 доска попадала в базу только как побочный след живого поиска
(`sources/catalog_sink.remember`): за две недели — одна карточка. Клиент,
которому бот отвечает из каталога, вьетнамского рынка байков не видел вовсе, а
это 10–20% разницы в цене (spec-v2, 7). Событий доска не шлёт, поэтому свежее
берётся опросом: раз в полчаса — первые страницы новейших объявлений по каждому
обслуживаемому городу и каждой категории, для которой у доски есть код.

Карточка собирается из того, что доска знает структурно (тип кузова, марка,
год — поля, которые заполняет сам продавец), и из текста — тем же разбором,
что читает телеграмные объявления и запросы клиентов. Структурное поле главнее
слова: продавец выбрал его из списка, а слово могло относиться к соседу.

Повторный обход старое не переписывает: `upsert_external` вставляет только
незнакомые `(source, external_id)`. Цена, изменённая продавцом, до базы не
доедет — записанный предел, а не недосмотр: карточка ведёт на оригинал.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import structlog

from sniffer.config import get_settings
from sniffer.domain.passport import Category
from sniffer.domain.records import Listing
from sniffer.search.intake_rules import parse_query
from sniffer.search.motorbike_models import BODY_SCOOTER
from sniffer.search.vocabulary import is_served
from sniffer.sources.base import RawItem, Source
from sniffer.sources.chat_directory import store_listings
from sniffer.sources.chotot import ChototSource
from sniffer.sources.chotot_reference import (
    CATEGORY_CG,
    MAX_LIMIT,
    MOTORBIKE_BRAND,
    MOTORBIKE_TYPE_AUTOMATIC,
    REGION_V2,
    SOURCE_NAME,
    TRANSMISSION_TYPE,
)

log = structlog.get_logger(__name__)

# Страниц новейших объявлений за обход. Замер 31.08.2026: в Нячанге всего 59
# объявлений категории — две страницы по 50 покрывают доску целиком; в Дананге
# их больше, и полчаса спустя первая страница снова свежая.
PAGES = 2

Store = Callable[[list[Listing]], Awaitable[int]]
SourceFactory = Callable[[], Source]
Clock = Callable[[], float]

# Обратные таблицы к справочнику доски: код поля → значение паспорта. Piaggio
# и Vespa делят код 3, обратно это Piaggio — марка, Vespa у неё модельный ряд.
_TRANSMISSION_BY_TYPE: dict[int, str] = {code: value for value, code in TRANSMISSION_TYPE.items()}
_BRAND_BY_CODE: dict[int, str] = {}
for _brand, _code in MOTORBIKE_BRAND.items():
    _BRAND_BY_CODE.setdefault(_code, _brand)


class ChototSync:
    """Один обход доски по расписанию. Возврат — сколько карточек добавлено."""

    def __init__(
        self,
        *,
        interval_s: float | None = None,
        source_factory: SourceFactory = ChototSource,
        store: Store = store_listings,
        clock: Clock = time.monotonic,
    ) -> None:
        self._interval = (
            float(get_settings().chotot_sync_interval_s) if interval_s is None else interval_s
        )
        self._source_factory = source_factory
        self._store = store
        self._clock = clock
        # Первый обход — сразу при старте процесса: деплой не должен оставлять
        # каталог без доски на полчаса.
        self._due_at = 0.0

    async def tick(self) -> int:
        now = self._clock()
        if now < self._due_at:
            return 0
        self._due_at = now + self._interval
        stored = 0
        for city in cities():
            for category in CATEGORY_CG:
                stored += await self._sync(city, category)
        log.info("chotot.synced", stored=stored)
        return stored

    async def _sync(self, city: str, category: Category) -> int:
        adapter = self._source_factory()
        found: list[RawItem] = []
        try:
            for page in range(PAGES):
                items = await adapter.search(
                    "",
                    {
                        "city": city,
                        "category": category.value,
                        "limit": MAX_LIMIT,
                        "offset": page * MAX_LIMIT,
                    },
                )
                if adapter.degraded:
                    # Адаптер по контракту не бросает: он вернул пусто и
                    # пометил себя. Дальше листать нечего — доска лежит.
                    log.warning("chotot.sync_degraded", city=city, category=category.value)
                    break
                found.extend(items)
                if len(items) < MAX_LIMIT:
                    break
        finally:
            await adapter.aclose()
        listings = [
            listing_from_ad(item, city=city, category=category)
            for item in found
            if item.posted_at is not None
        ]
        if not listings:
            return 0
        stored = await self._store(listings)
        log.info(
            "chotot.page_synced", city=city, category=category.value, read=len(found), new=stored
        )
        return stored


def cities() -> list[str]:
    """Города, где доска умеет искать и где ищем мы."""
    return [city for city in REGION_V2 if is_served(city)]


def listing_from_ad(item: RawItem, *, city: str, category: Category) -> Listing:
    """Объявление доски → карточка каталога. Дата обязательна — проверяет вызывающий."""
    posted = item.posted_at
    assert posted is not None
    text = f"{item.title}\n{item.text}".strip()
    attributes: dict[str, Any] = dict(parse_query(text).attributes)
    attributes.update(structured_facts(item.raw))
    return Listing(
        raw_message_id=None,
        source=SOURCE_NAME,
        external_id=item.external_id,
        # На доску несут продать: `st=s,k` — объявления о продаже.
        deal_type="sell",
        category=category.value,
        city=city,
        title=item.title or item.text[:180] or "объявление",
        summary=item.text or item.title,
        price_amount=Decimal(item.price_vnd) if item.price_vnd is not None else None,
        price_currency="VND" if item.price_vnd is not None else None,
        price_period="once" if item.price_vnd is not None else None,
        attributes=attributes,
        tg_link=item.url,
        lang="vi",
        # Выше телеграмной минимальной карточки (0.55): цена и категория здесь
        # структурные поля доски, а не догадка по тексту.
        confidence=0.8,
        posted_at=posted,
    )


def structured_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """Что продавец выбрал из списков доски: кузов и коробка, марка, год."""
    facts: dict[str, Any] = {}
    kind = _as_int(raw.get("motorbiketype"))
    transmission = _TRANSMISSION_BY_TYPE.get(kind) if kind is not None else None
    if transmission:
        facts["transmission"] = transmission
    if kind == MOTORBIKE_TYPE_AUTOMATIC:
        facts["body_type"] = BODY_SCOOTER
    brand = _BRAND_BY_CODE.get(_as_int(raw.get("motorbikebrand")) or 0)
    if brand:
        facts["brand"] = brand
    year = _as_int(raw.get("regdate"))
    if year is not None and 1950 < year < 2100:
        facts["year"] = year
    return facts


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except ValueError:
        return None
