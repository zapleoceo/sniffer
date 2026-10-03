"""Площадь и этаж жилья из текста: что считается, а что нет.

Каждый случай — реальный шаблон из чатов (16 600 постов жилья, 03.10.2026), сокращённый
до строк, на которых он держится. Оба числа легко прочесть неверно: у дома несколько
площадей, у квартиры — площади комнат, а «5 этажей» — это этажность, а не этаж.
"""

from __future__ import annotations

import pytest

from sniffer.domain.facts_area import read_area
from sniffer.domain.facts_floor import read_floor, read_storeys
from sniffer.domain.facts_text import fact_text


def area(text: str) -> float | None:
    return read_area(fact_text(text))


def floor(text: str) -> int | None:
    return read_floor(fact_text(text))


def storeys(text: str) -> int | None:
    return read_storeys(fact_text(text))


def case(name: str, text: str, expected: object) -> object:
    return pytest.param(text, expected, id=name)


AREAS = [
    case("label_and_unit", "📐 Площадь: 38 м2", 38),
    case("label_with_the_square_sign", "📐 Площадь: 90 м² – 3 этаж", 90),
    case("label_without_a_colon", "Площадь 45м2", 45),
    case("unit_only_in_a_headline", "Квартира Hon Chong, 1 спальня, 40м², 15 млн", 40),
    case("sqm", "Area: 60m2 | 2 Bedrooms", 60),
    case("viet_label_with_a_bare_m", "Diện tích : 40m - Có Ban Công", 40),
    case("decimal_comma", "площадь 38,5 кв.м", 38.5),
    case("short_label_form", "Площадь: 35 кв", 35),
    case("range_takes_the_lower_bound", "студия 25-30 м2", 25),
    case("total_beats_a_plain_label", "Площадь: 130 м² | Полезная площадь: 250 м²", 250),
    case("viet_usable_beats_land", "Diện tích đất 440m² – diện tích sử dụng hơn 450m²", 450),
    case("plain_label_beats_a_bare_unit", "студия 20 м2\nПлощадь: 35 м2", 35),
    case("whole_lot_after_a_comma", "Вилла с садом, спальни, 180 м², An Vien", 180),
    case("first_of_equals_wins", "Площадь: 35 м2\nArea: 45 m2", 35),
    case("composition_before_the_area", "1 bedroom (50 м², full option)", 50),
    case("two_digits_lower_bound", "площадь 8 м2", 8),
]
NOT_AREAS = [
    case("land_plot", "• Площадь участка: 300 м² — фасад 10 м", None),
    case("viet_land", "Diện tích đất: 75m² (ngang 5m, dài 15m)", None),
    case("a_bedroom_is_not_the_flat", "спальня 15 м2, кухня 10 м2", None),
    case("a_balcony_is_not_the_flat", "Балкон 6 м2", None),
    case("lot_dimensions", "Площадь: 5м × 13м", None),
    case("price_written_as_an_area", "Площадь: 40 млн/месяц", None),
    case("too_small", "площадь 5 м2", None),
    case("too_big", "площадь 1500 м2", None),
    case("a_thousands_tail_is_not_an_area", "площадь 1 200 м2", None),
    case("a_distance_is_not_an_area", "до моря 100 м", None),
    case("the_plaza_is_not_an_area", "До площади 2/4 — около 500 м", None),
    case("silence", "Сдаётся студия у моря", None),
]


@pytest.mark.parametrize(("text", "expected"), AREAS + NOT_AREAS)
def test_the_area_of_the_lot_is_read_or_left_unsaid(text: str, expected: float | None) -> None:
    assert area(text) == expected


FLOORS = [
    case("ordinal_and_noun", "Расположена на 5 этаже", 5),
    case("hyphen_ordinal", "6-й этаж, есть лифт", 6),
    case("hyphen_ordinal_genitive", "с 5-го этажа открывается вид", 5),
    case("label_and_number", "Этаж: 3 (есть лифт)", 3),
    case("label_and_a_fraction", "этаж 2/4", 2),
    case("label_of", "Этаж 5 из 25", 5),
    case("fraction_with_a_noun", "5/12 этаж", 5),
    case("range_takes_the_lower", "1–2 этаж, без лифта", 1),
    case("english_ordinal", "7th Floor, fully furnished", 7),
    case("english_label", "Floor: 12", 12),
    case("ground_floor", "Ground Floor, 40 м2", 1),
    case("word_ordinal", "Квартира на первом этаже, 60 м2", 1),
    case("viet_tang", "căn tầng 4 , không ban công", 4),
    case("viet_ground", "Lối đi Thang bộ tầng trệt", 1),
    case("viet_lau_is_one_higher", "(Lầu 1)", 2),
    case("southern_and_russian_agree", "4-й этаж (Lầu 3 / 4th floor)", 4),
    case("the_same_floor_twice", "Этаж: 2\n2 floor", 2),
]
NOT_FLOORS = [
    case("storeys_of_a_building", "Здание 25 этажей, лифт", None),
    case("a_house_by_floors", "3 этажа, 4 спальни", None),
    case("different_floors_is_a_catalog", "Свободна на 3 этаже и на 4 этаже", None),
    case("the_pool_floor", "Бассейн на 31 этаже только для жителей", None),
    case("the_washer_floor", "🧺 Общая стиральная машина на 8 этаже", None),
    case("the_terrace_floor", "có sân thượng trên tầng 4", None),
    case("a_gift_is_not_a_floor", "Tặng 1 tháng tiền nhà", None),
    case("the_lift_goes_up_to", "Лифт до 3 этажа, далее лестница", None),
    case("no_number", "Высокий этаж, вид на море", None),
    case("too_high", "Этаж: 99", None),
]


@pytest.mark.parametrize(("text", "expected"), FLOORS + NOT_FLOORS)
def test_the_floor_of_a_flat_is_read_or_left_unsaid(text: str, expected: int | None) -> None:
    assert floor(text) == expected


STOREYS = [
    case("by_adjective", "🏡 СДАЁТСЯ ЦЕЛЫЙ 3-ЭТАЖНЫЙ ДОМ — 4 СПАЛЬНИ", 3),
    case("by_a_noun_in_the_plural", "🛏 4 спальни | 🛁 4 санузла | 3 этажа + терраса", 3),
    case("by_a_label", "Этажность: 4", 4),
    case("english_floors", "House 3 floors: 4 Bedroom 4WC", 3),
    case("english_storey", "3-storey house for rent", 3),
    case("viet_tang_count", "Kết cấu: 2 tầng, 1 tum", 2),
    case("two_counts_that_disagree", "2 этажа и 3 этажа", None),
    case("no_count", "Дом с садом", None),
]


@pytest.mark.parametrize(("text", "expected"), STOREYS)
def test_the_number_of_storeys_of_a_house_is_read_separately(
    text: str, expected: int | None
) -> None:
    """У дома «3 этажа» — этажность, и дом не стоит на третьем этаже."""
    assert storeys(text) == expected
    assert floor(text) is None
