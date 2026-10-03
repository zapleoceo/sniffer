"""Враждебный текст не останавливает воронку: 4 КБ — меньше полутора секунд.

Воронка обрабатывает сообщения по одному, и одно кривое не смеет её остановить. Образец —
`test_prices.py`: прогон 4-килобайтных текстов нашёл там строку из тысячи чисел, которая
считалась по четыре секунды. Здесь те же приёмы, но против новых чтений: повторяющиеся
метки, отрицания, этажи, площади, пробелы после слова, строки без концов.
"""

from __future__ import annotations

import time

import pytest

from sniffer.domain.facts_amenities import amenity_facts
from sniffer.domain.facts_text import MAX_TEXT, fact_text
from sniffer.domain.listing_title import MAX_TITLE
from sniffer.domain.text_lang import detect_lang
from sniffer.pipeline.listing_facts import fact_columns

POISON_PILLS = {
    "negations": "без балкона, " * 320,
    "negation_lists": "без " + "балкона, лифта и " * 250,
    "absent_after": "лифт нет " * 450,
    "spaces_after_a_word": "балкон" + " " * 3900,
    "floors": "1 этаж " * 570,
    "floor_fractions": "5 / 12 этаж " * 330,
    "area_labels": "площадь: 35 " * 330,
    "area_units": "35 м2 " * 660,
    "area_ranges": "35-40 " * 660,
    "deposit_digits": "залог " + "1 " * 1990,
    "deposit_labels": "залог 1 " * 490,
    "term_labels": "договор 3 " * 400,
    "term_fillers": "контракт " + "слово " * 600,
    "sea_phrases": "5 минут до моря " * 250,
    "sea_endless": "до моря " + "минут " * 600,
    "mileage_labels": "пробег 1 " * 440,
    "years": "2019 " * 790,
    "one_place_many_times": "Фыок Хай " * 440,
    "one_zone_many_times": "север нячанга " * 280,
    "location_labels": "📍 Район: " * 400,
    "pets": "животные " * 440,
    "one_endless_number": "9" * 4000,
    "one_endless_word": "а" * 4000,
    "only_spaces": " " * 4000,
    "only_newlines": "\n" * 4000,
    "brackets": "(" * 4000,
    "hyphens": "-" * 4000,
    "hashtags": "#" * 4000,
    "emoji": "😀" * 2000,
    "grouped_numbers": "1.000.000 " * 400,
    "math_digits": "𝟙" * 3000,
    "keycaps": "1️⃣" * 1300,
}
CATEGORIES = (("apartment", "rent_out"), ("house", "rent_out"), ("motorbike", "sell"))


@pytest.mark.parametrize("text", POISON_PILLS.values(), ids=POISON_PILLS.keys())
def test_a_hostile_text_neither_crashes_nor_stalls_the_funnel(text: str) -> None:
    started = time.perf_counter()

    for category, deal in CATEGORIES:
        columns = fact_columns(text[:4096], category=category, deal_type=deal)
        assert len(columns.title) <= MAX_TITLE + 1 or columns.title == "Объявление"
    detect_lang(text)

    assert time.perf_counter() - started < 1.5


def test_the_limits_that_keep_the_funnel_fast_are_part_of_the_contract() -> None:
    """Предел текста — часть договора: простыня читается только в пределах `MAX_TEXT`.

    Факт за пределом не читается — как и цена после шести тысяч знаков: объявление, у
    которого главное стоит после простыни, не объявление, и читать её дальше значит
    платить временем воронки.
    """
    near = fact_text("Балкон\n" + "x" * 100).folded
    far = fact_text("x" * (MAX_TEXT + 10) + "\nБалкон").folded

    assert amenity_facts(fact_text("Балкон\n" + "x" * 100)) == {"balcony": True}
    assert "балкон" in near
    assert "балкон" not in far
