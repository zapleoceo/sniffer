"""Паспорт обещает одно, карточка несёт другое — и расхождение ловится механически.

R3, 03.10.2026: паспорт называл `area_m2`, `floor`, `elevator`, `balcony`, `pool`… а карточка
их не несла, поэтому SQL-фильтр каталога умел всего четыре атрибута. «Это не „нужно больше
извлекать“, а „извлекать то, что уже обещано“». Чтобы словари не разъезжались снова,
каждый ключ, который читают факты, либо назван в `CATEGORY_ATTRIBUTES`, либо назван здесь
явно как «только карточка»; и каждый обещанный паспортом ключ жилья либо читается, либо
назван явно как «пока не читается». Новый ключ без решения роняет тест.
"""

from __future__ import annotations

from sniffer.domain.facts_bikes import bike_facts
from sniffer.domain.facts_housing import housing_facts
from sniffer.domain.facts_text import fact_text
from sniffer.domain.passport import CATEGORY_ATTRIBUTES, Category

RICH_HOME = """\
Сдаётся 2-комнатная квартира в Oceanus, 5 этаж, студия
📐 Площадь: 65 м2, отдельная кухня
Балкон, лифт, бассейн, спортзал, стиральная машина, кондиционер. Можно с животными.
Депозит 1 месяц, контракт от 3 месяцев
5 минут до моря, 300 метров до пляжа
Дом 3 этажа
"""
RICH_BIKE = "Honda Vision, 2019 год\nПробег 15 000 км\nБлю кард есть, права не нужны, торг"

# Читает разбор запроса (`search.intake_rules`), а не факты: воронка кладёт их в атрибуты сама.
FROM_THE_QUERY_PARSER = {"rooms", "furnished", "sea_view"}
# Ключи, которые карточка несёт, а паспорт о них пока не спрашивает.
LISTING_ONLY = {"zone", "kitchen", "sea_distance_min", "sea_distance_m", "floors_total"}
# Обещано паспортом жильём, но из текста пока не читается — решение, а не недосмотр.
NOT_READ_YET = {"utilities_included"}
BIKE_LISTING_ONLY = {"year", "mileage_km", "no_license_claimed", "bargain"}


def housing_keys(category: str, deal_type: str = "rent_out") -> set[str]:
    return set(housing_facts(fact_text(RICH_HOME), category=category, deal_type=deal_type))


def test_every_housing_fact_is_a_passport_attribute_or_named_card_only() -> None:
    promised = set(CATEGORY_ATTRIBUTES[Category.APARTMENT]) | set(
        CATEGORY_ATTRIBUTES[Category.ROOM]
    )
    promised |= set(CATEGORY_ATTRIBUTES[Category.HOUSE])

    for category in ("apartment", "room", "house"):
        unknown = housing_keys(category) - promised - LISTING_ONLY
        assert not unknown, (
            f"{category}: ключи без решения (паспорт или «только карточка»): {unknown}"
        )


def test_every_attribute_the_passport_promises_to_flats_is_read_or_named_not_read_yet() -> None:
    read = housing_keys("apartment") | FROM_THE_QUERY_PARSER
    promised = set(CATEGORY_ATTRIBUTES[Category.APARTMENT])

    assert promised - read - NOT_READ_YET == set()
    assert NOT_READ_YET <= promised, "«пока не читается» устарело: ключ уже не в паспорте"


def test_a_house_is_promised_what_a_house_has_and_not_what_a_flat_has() -> None:
    """Этаж у дома — этажность; «этаж 5» у дома не бывает атрибутом паспорта."""
    house = set(CATEGORY_ATTRIBUTES[Category.HOUSE])

    assert "floors_total" in house
    assert "floor" not in house
    assert house - set(CATEGORY_ATTRIBUTES[Category.APARTMENT]) == {"floors_total"}
    assert housing_keys("house") >= {"floors_total", "area_m2"}
    assert "floor" not in housing_keys("house")


def test_every_bike_fact_is_a_passport_attribute_or_named_card_only() -> None:
    keys = set(bike_facts(fact_text(RICH_BIKE), deal_type="sell"))

    assert keys - set(CATEGORY_ATTRIBUTES[Category.MOTORBIKE]) - BIKE_LISTING_ONLY == set()
    assert "papers" in keys, "документы — общее имя паспорта и карточки"


def test_a_lease_fact_is_read_only_for_a_lease() -> None:
    """Залог и срок есть у аренды; у продажи их нет, и «контракт» — не срок аренды."""
    sale = housing_keys("apartment", deal_type="sell")

    assert not sale & {"deposit_months", "min_term_months"}
