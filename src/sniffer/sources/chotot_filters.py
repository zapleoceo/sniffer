"""Атрибуты паспорта → структурные фильтры Chotot.

Зачем отдельный слой. Планировщик обязан оставаться источнико-независимым: он
не знает ни слова «chotot», ни поля `motorbiketype`. Поэтому план везёт факты
паспорта в нейтральном виде (`attributes`, `budget`), а перевести их в чужие
номера полей — работа адаптера. Добавится источник — здесь не меняется ничего;
добавится атрибут — меняются только те адаптеры, которые умеют его отбирать.

Почему это не текстовый запрос. Замер по Нячангу: общий запрос по категории —
42 настоящих скутера-автомата из 59 (71%), `motorbiketype=1` — 41 из 41 (100%).
Тип кузова отделяет скутер от спортбайка надёжнее любого слова, потому что это
не слово, а поле, которое заполняет сам продавец.

Незнакомое значение атрибута фильтром не становится и запрос не роняет: клиент,
сказавший «коробка неважна», должен получить выдачу, а не ошибку.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import structlog

from sniffer.domain.passport import Category
from sniffer.sources.chotot_reference import (
    CAPACITY_ABOVE_ALL,
    CAPACITY_BUCKETS,
    MOTORBIKE_BRAND,
    PRICE_CURRENCY,
    PRICE_MAX_VND,
    PRICE_PARAM,
    REGDATE_PARAM,
    TRANSMISSION_TYPE,
)

log = structlog.get_logger(__name__)

# Один атрибут — одна функция перевода. Таблица, а не цепочка `if`: новый
# атрибут это строка здесь, а не ветка в общем разборе.
AttributeFilter = Callable[[Any], dict[str, Any]]


def _transmission(value: Any) -> dict[str, Any]:
    code = TRANSMISSION_TYPE.get(str(value).strip().lower())
    return {"motorbiketype": code} if code else {}


def _engine_cc(value: Any) -> dict[str, Any]:
    cc = _as_int(value)
    if cc is None or cc <= 0:
        return {}
    for upper, code in CAPACITY_BUCKETS:
        if cc <= upper:
            return {"motorbikecapacity": code}
    return {"motorbikecapacity": CAPACITY_ABOVE_ALL}


def _brand(value: Any) -> dict[str, Any]:
    code = MOTORBIKE_BRAND.get(str(value).strip().lower())
    return {"motorbikebrand": code} if code else {}


def _year_min(value: Any) -> dict[str, Any]:
    year = _as_int(value)
    # Верхняя граница не проверяется: год из будущего просто вернёт ноль
    # объявлений, а это честный ответ на запрос «не старше 2030 года».
    return {REGDATE_PARAM: year} if year and year > 1900 else {}


ATTRIBUTE_FILTERS: dict[Category, dict[str, AttributeFilter]] = {
    Category.MOTORBIKE: {
        "transmission": _transmission,
        "engine_cc": _engine_cc,
        "brand": _brand,
        "year_min": _year_min,
    },
}


def attribute_params(category: Any, attributes: Any) -> dict[str, Any]:
    """Фильтры Chotot для тех атрибутов, которые он умеет отбирать.

    Категория нужна потому, что `motorbiketype` существует только у `cg=2020`:
    отправить его в объявления о квартирах — гарантированный ноль.
    """
    resolved = _as_category(category)
    if resolved is None or not isinstance(attributes, Mapping):
        return {}
    filters = ATTRIBUTE_FILTERS.get(resolved, {})
    built: dict[str, Any] = {}
    for attribute, value in attributes.items():
        translate = filters.get(str(attribute))
        if translate is not None:
            built.update(translate(value))
    return built


def budget_params(budget: Any) -> dict[str, Any]:
    """Бюджет — в фильтр цены, но только когда он уже в донгах.

    Курс USD→VND сюда намеренно не вписан: захардкоженный курс протухает молча
    и превращает верный фильтр в неверный, а Chotot принимает только донги.
    Пересчёт появится вместе с источником курса, а до тех пор бюджет в валюте
    отсекается ранжированием, а не запросом.

    Случай при этом не редкий, а основной: клиент почти всегда говорит «до 400»
    в долларах, значит этот фильтр пуст в БОЛЬШИНСТВЕ запросов. Цена видна
    замером: `motorbiketype=1` — 41 объявление, он же с `price=0-25000000` — 30.
    Поэтому источник курса — задача P1 (spec-v2, 2.2 и 8), а не «когда-нибудь».
    """
    if not isinstance(budget, Mapping):
        return {}
    currency = str(budget.get("currency") or "").upper()
    if currency != PRICE_CURRENCY:
        return {}
    low = _as_int(budget.get("min")) or 0
    high = _as_int(budget.get("max")) or PRICE_MAX_VND
    if low >= high:
        log.warning("chotot.budget_inverted", low=low, high=high)
        return {}
    return {PRICE_PARAM: f"{low}-{high}"}


def _as_category(value: Any) -> Category | None:
    if isinstance(value, Category):
        return value
    try:
        return Category(value)
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return int(float(value))
    except ValueError:
        return None
