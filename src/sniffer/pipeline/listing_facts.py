"""Факты карточки: что из текста объявления идёт в колонки и в атрибуты.

Как `listing_price` раскладывает цену, так этот модуль раскладывает остальное, что пост
говорит о лоте: район и зона, язык, заголовок и атрибуты (площадь, этаж, удобства,
условия аренды у жилья; год, пробег, документы, торг у байка). Все чтения — чистые
функции из `domain/` (`facts_*`, `listing_title`, `text_lang`), здесь они только
собраны вместе, чтобы воронка и будущий пересчёт накопленного звали одно место.

Правило слияния: явное значение из разбора запроса (`parse_query` — число комнат,
мебель, вид на море, марка, объём) главнее прочитанного здесь, если оно непустое. Факты
дополняют, а не перечитывают то, что воронка уже знает: второй список слов для той же
мебели разъехался бы с первым.

Контактов тут нет: телефон, @username и Zalo в карточку не попадают никогда. В ней
стоит ссылка на оригинал, а чужие контакты в базе — лишний риск без пользы.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from sniffer.domain.districts import CITY_DATA, PLACE_BY_SLUG
from sniffer.domain.facts_bikes import bike_facts
from sniffer.domain.facts_housing import housing_facts
from sniffer.domain.facts_place import PlaceFact, read_place
from sniffer.domain.facts_text import fact_text
from sniffer.domain.listing_title import listing_title
from sniffer.domain.passport import RENTED_CATEGORIES, Category
from sniffer.domain.text_lang import detect_lang

_EMPTY: tuple[object, ...] = (None, "", [], {})


@dataclass(frozen=True, slots=True)
class FactColumns:
    """Значения колонок `title`, `district`, `lang` и полный набор атрибутов карточки."""

    title: str
    district: str | None = None
    lang: str | None = None
    attributes: dict[str, object] = field(default_factory=dict)


def fact_columns(
    text: str,
    *,
    category: str,
    deal_type: str,
    attributes: Mapping[str, object] | None = None,
    city: str = "",
) -> FactColumns:
    """Всё, что пост говорит о лоте, поверх уже известных атрибутов.

    `attributes` — то, что воронка узнала раньше (разбор запроса): оно главнее. `city` —
    город карточки: справочник районов и схема зон («север/центр/юг/запад») есть только у
    Нячанга и Дананга, а «в центре города» в ханойском объявлении зоной Нячанга не стала бы
    ни при каком чтении. Пустой город — город чата по умолчанию, то есть Нячанг.
    """
    prepared = fact_text(text)
    try:
        kind = Category(category)
    except ValueError:
        kind = Category.OTHER
    facts: dict[str, object] = {}
    if kind in RENTED_CATEGORIES:
        facts.update(housing_facts(prepared, category=category, deal_type=deal_type))
    elif kind is Category.MOTORBIKE:
        facts.update(bike_facts(prepared, deal_type=deal_type))
    place = read_place(prepared) if not city or city in CITY_DATA else PlaceFact()
    if place.zone:
        facts["zone"] = place.zone
    known = {key: value for key, value in (attributes or {}).items() if value not in _EMPTY}
    merged = {**facts, **known}
    named = PLACE_BY_SLUG.get(place.district or "")
    title = listing_title(
        text, category, merged, place_name=named.name if named else None, zone=place.zone
    )
    return FactColumns(title, place.district, detect_lang(text), merged)
