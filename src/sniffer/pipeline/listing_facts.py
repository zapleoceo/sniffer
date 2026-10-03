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
    """Значения колонок `title`, `district`, `city`, `lang` и полный набор атрибутов карточки."""

    title: str
    district: str | None = None
    city: str | None = None
    lang: str | None = None
    attributes: dict[str, object] = field(default_factory=dict)


MAX_DEPOSIT_MONTHS = 6


def deposit_in_months(amount: object, monthly_rent: int | None) -> int | float | None:
    """Залог суммой в месяцах аренды, с точностью до полумесяца; нелепое — `None`.

    Больше шести месяцев залога не просят: такое отношение — это не залог, а сумма за
    что-то другое или цена, записанная не в тех единицах.
    """
    if not isinstance(amount, int) or isinstance(amount, bool) or not monthly_rent:
        return None
    months = round(amount / monthly_rent * 2) / 2
    if not 0.5 <= months <= MAX_DEPOSIT_MONTHS:
        return None
    return int(months) if months == int(months) else months


def fact_columns(
    text: str,
    *,
    category: str,
    deal_type: str,
    attributes: Mapping[str, object] | None = None,
    city: str = "",
    monthly_rent: int | None = None,
) -> FactColumns:
    """Всё, что пост говорит о лоте, поверх уже известных атрибутов.

    `attributes` — то, что воронка узнала раньше (разбор запроса): оно главнее. `city` —
    город карточки: справочник районов и схема зон («север/центр/юг/запад») есть только у
    Нячанга и Дананга, а «в центре города» в ханойском объявлении зоной Нячанга не стала бы
    ни при каком чтении. Пустой город — город чата по умолчанию, то есть Нячанг.

    Город, который называет САМ пост (`FactColumns.city`), решение владельца 04.10.2026:
    нячангский чат с лотом из Дананга — карточка Дананга. Голос «Дананг» складывается из
    мест и слова в тексте (`facts_place._city`), а не из одного упоминания.

    `monthly_rent` — месячная аренда в донгах: залог суммой («депозит 18 млн») без неё
    остаётся суммой, а с ней становится и месяцами (`deposit_months`), которые умеет
    спрашивать паспорт. Прочитанные в тексте месяцы главнее пересчёта.
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
    if "deposit_months" not in merged:
        if months := deposit_in_months(merged.get("deposit_amount"), monthly_rent):
            merged["deposit_months"] = months
    named = PLACE_BY_SLUG.get(place.district or "")
    title = listing_title(
        text, category, merged, place_name=named.name if named else None, zone=place.zone
    )
    return FactColumns(title, place.district, place.city, detect_lang(text), merged)
