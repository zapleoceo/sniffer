"""Язык объявления — ru, en или vi — по письму и частотным словам.

У Telegram-архива `lang` не ставился вообще (пусто у всех 18 880 карточек). Каждый случай —
реальное начало поста; двуязычные посты — норма (русский + английский подряд), и клиент
читает такой пост по-русски.
"""

from __future__ import annotations

import pytest

from sniffer.domain.text_lang import detect_lang


def case(name: str, text: str, expected: str | None) -> object:
    return pytest.param(text, expected, id=name)


LANGUAGES = [
    case("russian", "Сдаётся квартира у моря, 2 спальни, залог 1 месяц", "ru"),
    case("russian_with_latin_names", "Квартира в Oceanus, 2 спальни на севере Нячанга", "ru"),
    case(
        "russian_and_english_twice",
        "Apartment for rent at Muong Thanh\n\nСдаётся квартира в комплексе Muong Thanh",
        "ru",
    ),
    case(
        "english",
        "Luxury studio apartment for rent - fully furnished - balcony - near the beach",
        "en",
    ),
    case(
        "english_with_vietnamese_names",
        "1-BEDROOM APARTMENT FOR RENT – MỸ ĐA ĐÔNG 12 | 14.5M",
        "en",
    ),
    case(
        "english_with_a_street_name",
        "📍 45 Nguyễn Đức An, Sơn Trà, Da Nang\n🛏 1 Bedroom\n💰 SALE PRICE",
        "en",
    ),
    case("vietnamese", "Cho thuê căn hộ 2 phòng ngủ, đầy đủ nội thất, gần biển", "vi"),
    case(
        "vietnamese_with_an_aggregator_line",
        "Cho thuê căn hộ CT2 VCN Phước Hải\nЦена: По запросу (1 комн.)\nCho thuê căn hộ CT2",
        "vi",
    ),
    case(
        "vietnamese_without_diacritics",
        "Cho thue can ho 2 phong ngu, day du noi that, gan bien",
        "vi",
    ),
    case("a_brand_title", "Honda Vision 2021", "en"),
    case("cyrillic_wins_when_it_is_most_of_the_text", "Продам скутер Honda", "ru"),
]
SILENT = [
    case("digits_only", "15 000 000 / 35 / 2021", None),
    case("emoji_only", "🔥🔥🔥 ⬜️⬜️", None),
    case("hashtags_and_links_only", "#нячанг https://example.com/page @someone", None),
    case("empty", "", None),
]


@pytest.mark.parametrize(("text", "expected"), LANGUAGES + SILENT)
def test_the_language_follows_the_letters_not_the_names(text: str, expected: str | None) -> None:
    assert detect_lang(text) == expected


def test_a_russian_footer_does_not_make_an_english_post_russian() -> None:
    """Четверть букв — порог: пара русских слов в конце английского поста его не меняет."""
    english = (
        "Fully furnished apartment for rent, two bedrooms, close to the beach, deposit one month. "
    )

    assert detect_lang(english * 3 + "Связаться") == "en"


def test_a_few_vietnamese_marks_do_not_make_an_english_post_vietnamese() -> None:
    assert detect_lang("Apartment for rent in Sơn Trà, with balcony and sea view") == "en"


def test_a_short_russian_word_in_an_english_name_is_not_russian() -> None:
    """Меньше двенадцати русских букв — это пометка, а не язык поста."""
    assert detect_lang("Сдам Honda Vision") == "en"


def test_a_short_russian_post_without_latin_is_russian() -> None:
    assert detect_lang("Сдам дом") == "ru"


def test_three_vietnamese_words_make_a_vietnamese_post_two_do_not() -> None:
    assert detect_lang("nha dep gan") == "vi"
    assert detect_lang("Nha dep Honda Vision") == "en"


def test_equal_word_counts_with_marks_are_vietnamese_without_marks_english() -> None:
    assert detect_lang("cho thuê căn hộ for rent") == "vi"
    assert detect_lang("nha dep gan bien for rent the and") == "en"


def test_vietnamese_words_with_marks_are_read_through_the_folded_form() -> None:
    assert detect_lang("for rent the cho thuê căn hộ gần biển") == "vi"
