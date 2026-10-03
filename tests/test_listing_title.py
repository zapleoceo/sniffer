"""Заголовок карточки: первая содержательная строка поста, а не первая непустая.

Каждый случай — реальное начало поста (замер 03.10.2026: «AN-HOME» — 1437 карточек из 18 868,
«#нячанг #аренда #сдам» — 486, ряды эмодзи, «📱 https://…», рекламные «Свободна и готова к
заселению!»). Клиент видел в выдаче десять карточек с одним и тем же словом вместо названия.
"""

from __future__ import annotations

import pytest

from sniffer.domain.listing_title import (
    MAX_TITLE,
    NO_TITLE,
    content_title,
    facts_title,
    listing_title,
)


def case(name: str, text: str, expected: str | None) -> object:
    return pytest.param(text, expected, id=name)


TITLES = [
    case(
        "plain_first_line",
        "Дом 3 спальни в Нам Нячанг, 8 млн\nРайон: Nam Nha Trang",
        "Дом 3 спальни в Нам Нячанг, 8 млн",
    ),
    case(
        "emoji_edges_are_cut",
        "❤️ Квартира у моря в Oceanus, 2 спальни ❤️\nЦена",
        "Квартира у моря в Oceanus, 2 спальни",
    ),
    case(
        "agency_brand_and_a_tagline",
        "AN-HOME\nаренда начинается здесь…\n\n🌿 Дом с 5 спальнями в Центре Нячанга 🌴",
        "Дом с 5 спальнями в Центре Нячанга",
    ),
    case(
        "a_lone_brand",
        "LVCC\n🔠🔠🔠\nСдаётся 2-комнатная квартира в Oceanus",
        "Сдаётся 2-комнатная квартира в Oceanus",
    ),
    case(
        "brand_in_capitals",
        "OCEANUS 🇻🇳\n2-КОМНАТНАЯ КВАРТИРА В АРЕНДУ НА СЕВЕРЕ НЯЧАНГА 🌊",
        "2-КОМНАТНАЯ КВАРТИРА В АРЕНДУ НА СЕВЕРЕ НЯЧАНГА",
    ),
    case(
        "hashtags_first",
        "#нячанг #аренда #сдам\nСдаётся уютная студия с балконом",
        "Сдаётся уютная студия с балконом",
    ),
    case(
        "a_link_first",
        "📱 https://t.me/example 🖥\n🔠🔠🔠🔠🔠\nHouse 3 floors: 3 Bedroom 4WC 85m2",
        "House 3 floors: 3 Bedroom 4WC 85m2",
    ),
    case("emoji_only", "😏🙄😍😜😉😏\n⬜️⬜️⬜️⬜️⬜️\nСтудия у моря, 35 м2", "Студия у моря, 35 м2"),
    case(
        "letters_drawn_with_emoji",
        "1️⃣➖🅱️🔠🔠🔠🔠🔠🔠\nАРЕНДА 1-СПАЛЬНОЙ КВАРТИРЫ НА TRAN QUY CAP",
        "АРЕНДА 1-СПАЛЬНОЙ КВАРТИРЫ НА TRAN QUY CAP",
    ),
    case(
        "available_from_a_date",
        "Свободна с 8 октября\nСдаётся студия на Hùng Vương",
        "Сдаётся студия на Hùng Vương",
    ),
    case(
        "available_and_ready",
        "Свободна и готова к заселению!\nКрасивый дом в южном районе Нячанга",
        "Красивый дом в южном районе Нячанга",
    ),
    case(
        "free_right_now",
        "🔥 СВОБОДНА СЕЙЧАС!\n2-комнатная квартира — Северный Нячанг",
        "2-комнатная квартира — Северный Нячанг",
    ),
    case(
        "a_promotional_banner",
        "💵Снимите дом в Нячанге без комиссии.💵\nСдаётся дом в My Gia",
        "Сдаётся дом в My Gia",
    ),
    case(
        "the_city_alone",
        "ВЬЕТНАМ НЯЧАНГ\nСдаётся студия у моря с балконом",
        "Сдаётся студия у моря с балконом",
    ),
    case(
        "a_label_line",
        "Цена: 12 млн VND/мес\nСдаётся квартира в Phuoc Long, 45 м2",
        "Сдаётся квартира в Phuoc Long, 45 м2",
    ),
    case(
        "a_price_line",
        "14 000 000 VND/мес\nСдаётся квартира в Phuoc Long, 45 м2",
        "Сдаётся квартира в Phuoc Long, 45 м2",
    ),
    case(
        "a_building_label",
        "Здание: Oceanus\nСдаётся квартира в Oceanus, 45 м2",
        "Сдаётся квартира в Oceanus, 45 м2",
    ),
    case("a_bike_name_is_a_title", "🛵 SYM Attila Elisabeth\nЦена 9 млн", "SYM Attila Elisabeth"),
    case("a_short_title_with_a_number", "Студия 30м2\nЦена: 8 млн", "Студия 30м2"),
    case(
        "a_closing_bracket_stays",
        "‼️АРЕНДА БАЙКОВ (сутки/месяц)‼️\nSYM Atilla 50cc",
        "АРЕНДА БАЙКОВ (сутки/месяц)",
    ),
    case("math_digits_are_ordinary_digits", "Студия 𝟛𝟝 м2 у моря", "Студия 35 м2 у моря"),
]
NO_CONTENT = [
    case(
        "a_spec_card_has_no_title",
        "СДАМ КВАРТИРУ\nСтудия\nНЯЧАНГ СЕВЕР\n15 500 000 vnd\nЗалог: 1 месяц\nКонтракт: от 3 месяцев",  # noqa: E501
        None,
    ),
    case("only_hashtags", "#нячанг #аренда #сдам", None),
    case("only_emoji", "🔥\n⬜️⬜️⬜️", None),
    case("a_generic_frame", "Квартира в аренду в Нячанге\n2 спальни", None),
    case("empty", "", None),
]


@pytest.mark.parametrize(("text", "expected"), TITLES + NO_CONTENT)
def test_the_title_is_the_first_line_that_names_the_lot(text: str, expected: str | None) -> None:
    assert content_title(text) == expected


def test_a_long_line_is_cut_at_a_word_and_marked() -> None:
    line = "Сдаётся просторная светлая двухкомнатная квартира с видом на море в жилом комплексе у набережной"  # noqa: E501

    title = content_title(line)

    assert title is not None
    assert len(title) <= MAX_TITLE
    assert title.endswith("…")
    assert line.startswith(title.removesuffix("…"))
    assert not title.removesuffix("…").endswith(" ")


def test_a_short_line_is_left_whole_even_at_the_limit() -> None:
    line = "а" * 20 + " " + "б" * (MAX_TITLE - 21)

    assert content_title(line) == line


def test_a_line_of_one_endless_word_is_still_cut() -> None:
    title = content_title("Студия" + "ы" * 200 + " у моря")

    assert title is not None
    assert len(title) <= MAX_TITLE


def test_only_the_first_lines_are_searched() -> None:
    """Содержательная строка на десятой — уже не заголовок, а описание."""
    text = "\n".join(["#тег"] * 8) + "\nСдаётся хорошая квартира у моря"

    assert content_title(text) is None


def test_a_title_from_facts_names_the_kind_the_area_and_the_place() -> None:
    """Запасной заголовок — то, что пост сказал о лоте, а не пересказ его первой строки."""
    text = "СДАМ КВАРТИРУ\nСтудия\n15 500 000 vnd"

    title = listing_title(text, "apartment", {"area_m2": 35}, place_name=None, zone="north")

    assert title == "Студия · 35 м² · Север Нячанга"


def test_a_studio_needs_the_word_in_the_headline_not_just_one_room() -> None:
    """Однокомнатная — ещё не студия: `rooms=1` есть и у «1-комнатной», и у «студии»."""
    one_room = "СДАМ КВАРТИРУ\n1 спальня"
    studio = "СДАМ КВАРТИРУ\nСтудия"

    assert (
        facts_title("apartment", {"rooms": 1}, one_room, place_name=None, zone=None)
        == "1-комн. квартира"
    )
    assert facts_title("apartment", {"rooms": 1}, studio, place_name=None, zone=None) == "Студия"


def test_a_two_room_post_that_mentions_a_studio_in_a_menu_is_not_a_studio() -> None:
    text = "СДАМ КВАРТИРУ\n2 спальни\nМеню: студии и квартиры"

    assert (
        facts_title("apartment", {"rooms": 2}, text, place_name=None, zone=None)
        == "2-комн. квартира"
    )


def test_a_house_is_named_by_its_bedrooms_and_a_bike_by_its_make() -> None:
    assert (
        facts_title("house", {"rooms": 4}, "", place_name="My Gia", zone="west")
        == "Дом, 4 спальни · My Gia"
    )
    bike = {"brand": "honda", "model": "air_blade", "year": 2012}
    assert (
        facts_title("motorbike", bike, "", place_name=None, zone=None) == "Honda Air Blade · 2012"
    )


def test_the_place_name_beats_the_zone_name() -> None:
    title = facts_title("apartment", {"rooms": 2}, "", place_name="Phước Hải", zone="south")

    assert title == "2-комн. квартира · Phước Hải"


def test_a_bare_category_word_is_not_a_title() -> None:
    """«Квартира» без единого факта — не заголовок: лучше честное «Объявление»."""
    assert facts_title("apartment", {}, "", place_name=None, zone=None) is None
    assert listing_title("#нячанг", "apartment", {}) == NO_TITLE


def test_a_flag_is_not_an_area() -> None:
    """`True` — это ещё и число 1: булев атрибут не смеет стать «1 м²»."""
    title = facts_title("apartment", {"area_m2": True, "rooms": 2}, "", place_name=None, zone=None)

    assert title == "2-комн. квартира"


def test_the_content_line_wins_over_the_facts() -> None:
    title = listing_title("Дом в аренду в районе Май Гиа\n2 спальни", "house", {"rooms": 2})

    assert title == "Дом в аренду в районе Май Гиа"


def test_the_limit_is_eighty_characters_exactly() -> None:
    """80 знаков — целиком, 81 — с обрезкой; константа в тесте не используется намеренно."""
    words = "слово " * 20
    eighty = words[:80].rstrip() + "я" * (80 - len(words[:80].rstrip()))
    assert len(eighty) == 80

    assert content_title(eighty) == eighty
    cut = content_title(eighty + "я")
    assert cut is not None
    assert len(cut) <= 80
    assert cut.endswith("…")


def test_a_cut_does_not_fall_back_to_a_very_early_space() -> None:
    """Пробел на десятом знаке — не повод оставить «Квартира…»: режем по длине, не по слову."""
    title = content_title("Квартира " + "ы" * 200)

    assert title is not None
    assert len(title) == 80
    assert title.startswith("Квартира ыыыыыыыыыы")


def test_the_sixth_line_is_searched_and_the_seventh_is_not() -> None:
    lot = "Сдаётся хорошая квартира у моря"

    assert content_title("\n".join(["#тег"] * 5 + [lot])) == lot
    assert content_title("\n".join(["#тег"] * 6 + [lot])) is None


def test_a_mention_in_the_middle_of_a_line_is_dropped() -> None:
    assert content_title("@agency_rent Студия 30м2 у моря") == "Студия 30м2 у моря"


def test_a_price_line_in_plain_words_is_not_a_title() -> None:
    assert content_title("Only 35 triệu per month\nСтудия 30м2") == "Студия 30м2"


def test_a_phone_number_never_reaches_the_title() -> None:
    assert content_title("+84 901 234 567 Студия у моря 30м2") == "Студия у моря 30м2"


def test_a_link_never_reaches_the_title() -> None:
    assert content_title("https://example.com/x Студия 30м2 у моря") == "Студия 30м2 у моря"


def test_emoji_inside_a_line_are_dropped() -> None:
    assert content_title("Студия \U0001f525 30м2 \U0001f334 у моря") == "Студия 30м2 у моря"


def test_a_post_with_nothing_to_say_gets_the_plain_word() -> None:
    assert listing_title("#теги", "other", {}) == "Объявление"
