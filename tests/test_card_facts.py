"""Факты карточки из атрибутов: одна строка для чата, мониторинга и отложенных ответов."""

from __future__ import annotations

from sniffer.domain.card_facts import MAX_FACTS_LEN, facts_line

LEAD = {
    "brand": "honda",
    "model": "lead",
    "engine_cc": 110,
    "year": 2008,
    "transmission": "automatic",
}


def test_a_bike_reads_as_brand_model_volume_year_gearbox() -> None:
    assert facts_line(LEAD) == "Honda Lead · 110 cc · 2008 · автомат"


def test_what_the_title_already_says_is_not_repeated() -> None:
    line = facts_line(LEAD, title="Honda Lead 110cc срочно")
    assert line == "2008 · автомат"


def test_missing_attributes_print_nothing_at_all() -> None:
    assert facts_line(None) == ""
    assert facts_line({}) == ""
    assert facts_line({"brand": "", "engine_cc": 0, "year": None}) == ""


def test_a_flag_is_not_a_number_and_an_unknown_gearbox_is_not_printed() -> None:
    assert facts_line({"engine_cc": True, "transmission": "cvt-turbo"}) == ""


def test_mileage_and_area_use_thousands_with_a_non_breaking_space() -> None:
    line = facts_line({"mileage_km": 27800, "area_m2": 26.3})
    assert line == "27\u00a0800 км · 26.3 м²"


def test_a_long_garbage_value_cannot_inflate_the_card() -> None:
    line = facts_line({"brand": "x" * 500, "model": "y" * 500, "transmission": "manual"})
    assert len(line) <= MAX_FACTS_LEN


def test_an_absurd_number_cannot_inflate_the_card_either() -> None:
    line = facts_line({"brand": "x" * 500, "model": "y" * 500, "mileage_km": 10**60})
    assert len(line) <= MAX_FACTS_LEN and line.endswith("…")
