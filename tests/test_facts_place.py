"""Район и зона города из текста: где лот, а не что рядом с ним.

Каждый случай — реальный шаблон из чатов (16 600 постов жилья, 03.10.2026), сокращённый до
строк, на которых он держится. Главная ловушка — подвал: меню агентства называет чужие ЖК,
а «10 минут до центра города» называет центр, у которого лота нет.
"""

from __future__ import annotations

import pytest

from sniffer.domain.facts_place import PlaceFact, read_place
from sniffer.domain.facts_text import fact_text


def place(text: str) -> PlaceFact:
    return read_place(fact_text(text))


def case(name: str, text: str, district: str | None, zone: str | None) -> object:
    return pytest.param(text, district, zone, id=name)


FOUND = [
    case("labeled_value_and_zone", "📍 Район: Phước Hải — Южный Нячанг", "phuoc_hai", "south"),
    case(
        "label_with_a_bullet_list",
        "📍 Локация:\n• Запад Нячанга\n• My Gia\n• ≈ 5 минут до моря",
        "my_gia",
        "west",
    ),
    case(
        "bullet_list_stops_at_the_next_section",
        "📍 Локация:\n• Phuoc Long\n🟢 О квартире:\n• Oceanus",
        "phuoc_long",
        "south",
    ),
    case("pin_without_a_label", "Студия\n📍 Tân Lập – Нячанг", "tan_lap", "center"),
    case("headline_complex", "Квартира с 2 спальнями в Oceanus", "oceanus", "north"),
    case("complex_zone_from_the_reference", "Сдаётся студия в Gold Coast", "gold_coast", "center"),
    case("diacritics_and_capitals", "VĨNH ĐIỀM TRUNG, дом", "vinh_diem_trung", "west"),
    case("russian_spelling", "Квартира в Фыокхай, Нячанг", "phuoc_hai", None),
    case("russian_spelling_declined", "Студия в Фуок Хае", "phuoc_hai", None),
    case("russian_other_spelling", "Лок Тхо — центр города", "loc_tho", "center"),
    case("two_words_spelled_together", "Дом в районе Винь Дьем Чунг", "vinh_diem_trung", "west"),
    case(
        "explicit_zone_beats_the_reference",
        "Апартаменты в Ха Куанг 2 (Hà Quang 2) — Запад",
        "ha_quang",
        "west",
    ),
    case("the_other_explicit_zone", "Ha Quang 1 | Юг Нячанга", "ha_quang", "south"),
    case("standalone_word_after_a_dot", "Лок Тхо (loc tho) · центр", "loc_tho", "center"),
    case("ward_beats_the_complex", "📍 Локация: Oceanus, Phước Hải", "phuoc_hai", None),
    case("complex_beats_the_street", "📍 Район: Hùng Vương, Gold Coast", "gold_coast", "center"),
    case("only_a_street", "📍 Район: Trần Phú", "tran_phu", None),
    case("declined_after_a_consonant", "Студия в Ха Куанге", "ha_quang", None),
    case(
        "labeled_place_against_the_headline_zone",
        "Квартира в центре Нячанга\n📍 Район: Oceanus",
        "oceanus",
        "center",
    ),
    case(
        "labeled_circle_beats_the_headline",
        "Квартира в Oceanus\n2 спальни\n35 м2\n📍 Phuoc Hai",
        "phuoc_hai",
        None,
    ),
    case(
        "label_word_circle",
        "Квартира в Oceanus\n2 спальни\n35 м2\nРайон: Phuoc Hai",
        "phuoc_hai",
        None,
    ),
    case(
        "bulleted_list_under_a_label",
        "Квартира в Oceanus\n2 спальни\n35 м2\n📍 Локация:\n• Phuoc Long\n• 5 минут до моря",
        "phuoc_long",
        "south",
    ),
    case(
        "another_bullet_character",
        "Квартира в Oceanus\n2 спальни\n35 м2\n📍 Локация:\n➖ Phuoc Long",
        "phuoc_long",
        "south",
    ),
    case(
        "labeled_street_beats_a_body_ward",
        "Сдаётся студия\n2 спальни\n35 м2\n📍 Район: Tran Phu\nЖК Oceanus\nPhước Hải",
        "tran_phu",
        None,
    ),
    case("reform_ward", "📍 Phường Bắc Nha Trang", "bac_nha_trang", "north"),
    case("reform_ward_south", "Cho thuê nhà Nam Nha Trang", "nam_nha_trang", "south"),
    case("zone_only", "Сдаётся студия на севере Нячанга", None, "north"),
    case("zone_only_title", "НЯЧАНГ ЦЕНТР 12 500 000 vnd", None, "center"),
    case("zone_in_english", "Studio in the city center of Nha Trang", None, "center"),
    case("zone_west_in_viet", "Cho thuê nhà phía Tây Nha Trang", "tay_nha_trang", "west"),
    case("zone_south_phrase", "Дом в южной части Нячанга", None, "south"),
    case("zone_west_phrase", "Дом в западной части города", None, "west"),
]
NOT_FOUND = [
    case(
        "the_menu_is_not_the_address",
        "Дом с 3 спальнями\n──────\n👉 АРЕНДА В ЖК ОКЕАНУС\n👉 ДОМА И ВИЛЛЫ",
        None,
        None,
    ),
    case("a_hashtag_is_not_a_place", "Студия 30 м2 #oceanus #phuoclong", None, None),
    case("distance_to_the_centre", "Студия у моря, 10 минут до центра города", None, None),
    case("near_the_centre", "🛒 Рядом рынок, центр города и множество удобств", None, None),
    case("a_comparison", "Здесь тише, чем в центре — подходит для семьи", None, None),
    case("a_shopping_centre", "Рядом торговый центр Nha Trang Center", None, None),
    case("a_mall_in_the_city_name", "Апартаменты в торговом центре Нячанга", None, None),
    case("distance_from_the_centre", "Студия в 5 км от центра города", None, None),
    case("around_the_centre", "Студия около центра города", None, None),
    case("a_walk_from_the_centre_english", "Studio 10 minutes from the city center", None, None),
    case("a_trip_to_the_centre_english", "Studio, 5 km to the city center", None, None),
    case("accessibility_of_the_centre", "В шаговой доступности — центр города", None, None),
    case("a_comparative_is_not_a_zone", "Студия\n— севернее", None, None),
    case("window_orientation", "Ориентация: северо-восточная", None, None),
    case("window_orientation_english", "Bright, north-facing balcony", None, None),
    case("a_direction_of_travel", "Удобный выезд как в центр, так и на север города", None, None),
    case("central_air_conditioning", "tầng 1–2 điều hòa trung tâm", None, None),
    case("vietnam_is_not_south_nha_trang", "ВЬЕТНАМ НЯЧАНГ\nСдаётся студия", None, None),
    case("two_zones_in_one_circle", "Северный Нячанг, он же центр города", None, None),
    case("an_alias_inside_a_longer_word_left", "Chon Xen cafe", None, None),
    case("an_alias_inside_a_longer_word_right", "Hon Xenon Hotel", None, None),
    case("vietnam_in_latin_is_not_south_nha_trang", "Vietnam Nha Trang, studio", None, None),
    case("silence", "Сдаётся квартира, 10 млн", None, None),
]


@pytest.mark.parametrize(("text", "district", "zone"), FOUND + NOT_FOUND)
def test_the_place_of_the_lot_is_read_or_left_unsaid(
    text: str, district: str | None, zone: str | None
) -> None:
    found = place(text)

    assert (found.district, found.zone) == (district, zone)


def test_a_place_named_only_in_the_footer_against_the_headline_zone_is_a_menu_not_an_address() -> (
    None
):
    """LVCC пишет «центр Нячанга» в заголовке и перечисляет свои ЖК в конце."""
    head = "LVCC\n2-КОМНАТНАЯ КВАРТИРА В АРЕНДУ – ЦЕНТР НЯЧАНГА\n✨ 2 спальни\n"
    text = head + "Также в аренду: Oceanus, Scenia Bay"

    assert place(text) == PlaceFact(district=None, zone="center", city="nha_trang")


def test_a_place_named_in_the_footer_with_no_zone_in_the_headline_is_still_used() -> None:
    assert (
        place("Сдаётся студия\n2 спальни\n35 м2\nУдобства: бассейн\nВ ЖК Oceanus").district
        == "oceanus"
    )


def test_danang_is_named_by_its_districts_and_has_no_zone() -> None:
    """410 нячангских карточек называли районы Дананга; зоны севера и юга у Дананга нет."""
    found = place("📍 Location: The Ponte, Son Tra, Da Nang")

    assert found == PlaceFact(district="son_tra", zone=None, city="da_nang")


def test_the_card_of_one_city_may_name_the_place_of_another() -> None:
    """Город места не равен городу карточки — на этом «предложи, не меняй» и держится."""
    assert place("Studio for rent – Hai Chau District").city == "da_nang"
    assert place("Студия в Лок Тхо").city == "nha_trang"


def test_a_street_of_two_cities_loses_to_the_district_of_the_city_that_is_named() -> None:
    """«Trần Phú» есть и в Нячанге, и в Дананге: решает название района рядом."""
    found = place("📍 Address: Tran Phu, Hai Chau, Da Nang")

    assert (found.district, found.city) == ("hai_chau", "da_nang")


def test_a_chain_complex_of_two_cities_loses_to_an_explicit_city_name() -> None:
    """«Mường Thanh – Đà Nẵng»: Mường Thanh есть и в Нячанге, но пост назвал Дананг."""
    found = place("🔥 2-BEDROOM APARTMENT FOR RENT\nMƯỜNG THANH 🔥\n📍 Mường Thanh – Đà Nẵng")

    assert found.city == "da_nang"
    assert found.district is None


def test_the_earlier_named_city_wins_a_tie() -> None:
    assert place("📍 Район: Phuoc Hai\nСтудия рядом с Hai Chau").city == "nha_trang"
    assert place("📍 Район: Hai Chau\nСтудия рядом с Phuoc Hai").city == "da_nang"


def test_the_zone_of_a_danang_post_is_not_taken_from_nha_trang_words() -> None:
    assert place("Son Tra, Da Nang — центр города").zone is None


def test_decorative_lines_do_not_use_up_the_headline() -> None:
    """У AN-HOME и LVCC первые строки — ряды эмодзи; заголовок поста идёт за ними."""
    text = "⬜️⬜️⬜️\n🔥🔥🔥\n──────\nСдаётся студия в центре Нячанга\n2 спальни\nУдобства: бассейн\nВ ЖК Oceanus"  # noqa: E501

    assert place(text) == PlaceFact(district=None, zone="center", city="nha_trang")


def test_a_street_alone_does_not_decide_the_city_against_a_district() -> None:
    """Без слова «Дананг»: «Trần Phú» есть в обоих городах, а «Hải Châu» — только в одном."""
    found = place("📍 Address: Tran Phu, Hai Chau")

    assert (found.district, found.city) == ("hai_chau", "da_nang")
