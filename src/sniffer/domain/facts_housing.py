"""Факты жилья из текста объявления: что из этого найдено, а что — нет.

Склейка читателей по одному на группу фактов (`facts_amenities`, `facts_area`,
`facts_floor`, `facts_terms`, `facts_sea`). Имена атрибутов — те, что обещает паспорт
(`domain.passport.CATEGORY_ATTRIBUTES`): карточка называет то же, что можно спросить.

Чего здесь нет: числа комнат, мебели и вида на море. Их читает разбор запроса
(`search.intake_rules`), и воронка уже кладёт их в атрибуты карточки; второй список
слов для той же мебели разъехался бы с первым. Контактов тоже нет — и не будет: в
карточке стоит ссылка на оригинал, а телефон и @username в базе никому не нужны.
"""

from __future__ import annotations

from sniffer.domain.facts_amenities import amenity_facts
from sniffer.domain.facts_area import read_area
from sniffer.domain.facts_floor import read_floor, read_storeys
from sniffer.domain.facts_sea import read_sea_distance
from sniffer.domain.facts_terms import read_deposit_months, read_min_term_months
from sniffer.domain.facts_text import FactText

HOUSE = "house"
RENT_OUT = "rent_out"


def housing_facts(text: FactText, *, category: str, deal_type: str) -> dict[str, object]:
    """Площадь, этаж, удобства, условия аренды и расстояние до моря.

    Условия аренды (залог, срок) читаются только у предложения аренды: «контракт» в
    объявлении о продаже дома — не срок аренды. Этаж есть у квартиры и комнаты, а у
    дома вместо него этажность.
    """
    facts = amenity_facts(text)
    facts.update(read_sea_distance(text))
    if (area := read_area(text)) is not None:
        facts["area_m2"] = area
    if category == HOUSE:
        if (storeys := read_storeys(text)) is not None:
            facts["floors_total"] = storeys
    elif (floor := read_floor(text)) is not None:
        facts["floor"] = floor
    if deal_type == RENT_OUT:
        for key, value in (
            ("deposit_months", read_deposit_months(text)),
            ("min_term_months", read_min_term_months(text)),
        ):
            if value is not None:
                facts[key] = value
    return facts
