"""Chotot.vn — единственный источник, где вьетнамцы реально продают байки.

Без него теряется весь локальный рынок и 10–20% разницы в цене: в Telegram
вьетнамский сегмент отсутствует как явление (spec-v2, 7).

API недокументирован и может измениться молча, поэтому адаптер не доверяет
ответу ни в одном поле и на любой ошибке возвращает пустой список, пометив
себя `degraded` (spec-v2, 6.2). Коды регионов и категорий — в
`chotot_reference`, они снимаются опытом, а не выводятся.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from sniffer.config import get_settings
from sniffer.domain.passport import Category
from sniffer.sources.base import DEFAULT_TIMEOUT_S, RawItem, Source, register
from sniffer.sources.chotot_filters import attribute_params, budget_params
from sniffer.sources.chotot_reference import (
    AD_TYPE,
    AREA_V2,
    CATEGORY_CG,
    DEFAULT_LIMIT,
    GATEWAY_URL,
    LISTING_URL,
    MAX_LIMIT,
    PLAN_FILTERS,
    REGION_V2,
    SOURCE_NAME,
    USER_AGENT,
)

log = structlog.get_logger(__name__)


@register
class ChototSource(Source):
    name = SOURCE_NAME

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        super().__init__()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_s,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search(self, query: str, params: dict[str, Any]) -> list[RawItem]:
        request_params = build_params(query, params)
        try:
            response = await self._client.get(GATEWAY_URL, params=request_params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.degraded = True
            log.warning(
                "chotot.unavailable",
                error=f"{type(exc).__name__}: {exc}",
                params=request_params,
            )
            return []

        ads = payload.get("ads") if isinstance(payload, dict) else None
        if not isinstance(ads, list):
            self.degraded = True
            log.warning("chotot.unexpected_payload", payload_type=type(payload).__name__)
            return []

        items = [item for ad in ads if (item := _to_item(ad)) is not None]
        log.info("chotot.search", query=query, received=len(ads), mapped=len(items))
        return items


def build_params(query: str, params: dict[str, Any]) -> dict[str, Any]:
    """План поиска говорит «что искать» — здесь это становится query-string."""
    city = str(params.get("city") or get_settings().default_city)
    built: dict[str, Any] = {
        "limit": min(_as_int(params.get("limit")) or DEFAULT_LIMIT, MAX_LIMIT),
        "o": _as_int(params.get("offset")) or 0,
        "st": AD_TYPE,
    }

    cg = _as_int(params.get("cg")) or _category_cg(params.get("category"))
    if cg:
        built["cg"] = cg
    region = _as_int(params.get("region_v2")) or REGION_V2.get(city)
    if region:
        built["region_v2"] = region
    # Явный `area_v2: None` в плане осознанно расширяет поиск до провинции.
    area = _as_int(params.get("area_v2", AREA_V2.get(city)))
    if area:
        built["area_v2"] = area

    # Атрибуты паспорта — структурными полями, а не словами в `q`. Тип кузова
    # отделяет скутер от спортбайка точнее любого текста: замер по Нячангу —
    # 71% настоящих скутеров общим запросом против 100% с `motorbiketype=1`.
    built.update(attribute_params(params.get("category"), params.get("attributes")))
    built.update(budget_params(params.get("budget")))
    # Явный фильтр из плана важнее выведенного из паспорта: модель могла узнать
    # про источник то, чего в переводчике атрибутов ещё нет.
    built.update(_explicit_filters(params))

    if query.strip():
        built["q"] = query.strip()
    return built


def _explicit_filters(params: dict[str, Any]) -> dict[str, Any]:
    """Поля Chotot, названные в плане прямым именем."""
    return {key: params[key] for key in PLAN_FILTERS if params.get(key) is not None}


def _to_item(ad: Any) -> RawItem | None:
    if not isinstance(ad, dict):
        log.warning("chotot.ad_not_object", ad_type=type(ad).__name__)
        return None
    list_id = ad.get("list_id")
    if list_id is None:
        # ad_id есть всегда, но ссылка строится по list_id, а карточка без
        # ссылки на оригинал в выдачу не идёт — это весь смысл продукта.
        log.warning("chotot.ad_without_list_id", ad_id=ad.get("ad_id"))
        return None
    return RawItem(
        source=SOURCE_NAME,
        external_id=str(list_id),
        url=LISTING_URL.format(list_id=list_id),
        title=str(ad.get("subject") or ""),
        text=str(ad.get("body") or ""),
        price_raw=str(ad.get("price_string") or ""),
        price_vnd=_price_vnd(ad.get("price")),
        posted_at=_posted_at(ad),
        images=_images(ad),
        seller_name=str(ad.get("account_name") or ad.get("full_name") or ""),
        raw=ad,
    )


def _posted_at(ad: dict[str, Any]) -> datetime | None:
    """Дата берётся из list_time, а не из `date`.

    В `date` лежит человеческая фраза по-вьетнамски — «2 giờ trước»,
    «hôm qua», «1 tuần trước». Проверке живости (spec-v2, 3.3) нужна метка
    времени, а не текст, поэтому парсим list_time — эпоха в миллисекундах.
    `orig_list_time` — время первой публикации, есть не у всех и для свежести
    хуже: актуальна последняя публикация, а не исходная.
    """
    for key in ("list_time", "orig_list_time"):
        value = ad.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            continue
        try:
            return datetime.fromtimestamp(value / 1000, UTC)
        except (OverflowError, OSError, ValueError):
            log.warning("chotot.bad_timestamp", key=key, value=value)
    return None


def _price_vnd(value: Any) -> int | None:
    """Ноль и отрицательное ценой не считаем.

    Числовое поле у Chotot необязательное; человеку в таком случае показан
    текст, и он остаётся в `price_raw`.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    price = int(value)
    return price if price > 0 else None


def _images(ad: dict[str, Any]) -> list[str]:
    images = ad.get("images")
    if isinstance(images, list):
        urls = [url for url in images if isinstance(url, str) and url]
        if urls:
            return urls
    single = ad.get("image")
    return [single] if isinstance(single, str) and single else []


def _category_cg(value: Any) -> int | None:
    """Категория паспорта → cg Chotot. Незнакомая — не повод падать."""
    if value is None:
        return None
    try:
        return CATEGORY_CG.get(Category(value))
    except ValueError:
        log.warning("chotot.unknown_category", category=value)
        return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except ValueError:
        return None
