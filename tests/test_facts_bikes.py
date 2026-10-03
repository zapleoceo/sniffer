"""Факты мотобайка из текста: год, пробег, документы, «права не нужны», торг.

Каждый случай — реальный шаблон из чатов (2051 пост продажи, 03.10.2026), сокращённый до
строк, на которых он держится. У байка чисел много, и почти каждое — не то, чем кажется:
«до 100 км от города» — не пробег, «до 60 км/ч» — не пробег, «2 000 000» — не год.
"""

from __future__ import annotations

import pytest

from sniffer.domain.facts_bikes import bike_facts, read_mileage_km, read_year
from sniffer.domain.facts_text import fact_text


def sale(text: str) -> dict[str, object]:
    return bike_facts(fact_text(text), deal_type="sell")


def case(name: str, text: str, expected: object) -> object:
    return pytest.param(text, expected, id=name)


YEARS = [
    case("year_and_a_word", "Honda PCX, 2016 год · цвет: Темно-синий", 2016),
    case("year_genitive", "Продаю Honda PCX 125, 2011 года.", 2011),
    case("year_with_g", "Honda AirBlade 125cc 2016г.вып.", 2016),
    case("year_with_a_dot_g", "Yamaha NVX 125, 2017 г.", 2017),
    case("year_label", "Год выпуска: 2024", 2024),
    case("year_label_in_brackets", "📅 Год: (2011г)", 2011),
    case("registration_year", "Год первой регистрации: 2009", 2009),
    case("english_label", "Year: 2019 | Mileage: 20,000 km", 2019),
    case("bare_year_in_the_title", "SYM Elizabeth , 2011\nБлю кард есть", 2011),
    case("bare_year_after_a_model", "Honda Air Blade FI 2011. Инжекторный.", 2011),
    case("bare_year_in_brackets", "Yamaha Nouvo 6 (2016)\nПродаю в связи с переездом", 2016),
    case("strong_beats_bare", "Honda Vision 2012\nГод выпуска: 2011", 2011),
    case("purchase_date_is_not_a_year", "Куплен в мае 2026 года новым\nHonda Lead 2019", 2019),
]
NOT_YEARS = [
    case("a_price", "Цена 2.025.000 VND", None),
    case(
        "a_year_in_the_footer_only", "Продаю байк\nЦена 9 млн\nКонтакты ниже\nОбновлено 2025", None
    ),
    case("a_mileage", "пробег 2000 км", None),
    case("a_date", "до 01.10.2024", None),
    case("too_old", "Honda Cub 1983", None),
    case("too_new", "Honda 2035", None),
    case(
        "the_unit_after_the_title_lines",
        "Продаю Honda Vision\nЦена 15 млн\nСостояние отличное\n2019 год выпуска",
        2019,
    ),
]


@pytest.mark.parametrize(("text", "expected"), YEARS + NOT_YEARS)
def test_the_model_year_is_read_with_a_label_or_in_the_title(
    text: str, expected: int | None
) -> None:
    assert read_year(fact_text(text)) == expected


MILEAGE = [
    case("label_and_km", "· пробег: 54.000 км · объём: 110 куб", 54000),
    case("label_with_a_space", "Пробег: 52 560 км", 52560),
    case("label_dash", "Пробег — 31 000 км.", 31000),
    case("label_about", "Пробег ~19 500 км.", 19500),
    case("label_thousands", "Пробег: ~33 тыс. км", 33000),
    case("label_k", "пробег 40к, 20.5млн", 40000),
    case("label_bare", "пробег 32000", 32000),
    case("label_all_in_all", "Пробег всего 3000 км.", 3000),
    case("english_odo", "🛞 ODO: 32.172 km", 32172),
    case("dotted_without_a_unit", "· пробег: 29.500", 29500),
]
NOT_MILEAGE = [
    case("a_distance_from_the_town", "не дальше 100 км от города", None),
    case("a_speed", "максимальная скорость 60 км/ч", None),
    case("a_speed_after_the_label", "Пробег небольшой, едет 60 км/ч", None),
    case(
        "a_number_far_from_the_label",
        "Пробег смотрите на фото, байк в отличном состоянии, до города 100 км",
        None,
    ),
    case("a_range_on_a_charge", "запас хода 100 км", None),
    case("a_bare_small_number_after_the_label", "пробег 55", None),
    case("too_far", "пробег 900 000 км", None),
    case("a_service_interval", "масло менялось 500 км назад", None),
]


@pytest.mark.parametrize(("text", "expected"), MILEAGE + NOT_MILEAGE)
def test_the_mileage_is_read_only_after_the_word_mileage(text: str, expected: int | None) -> None:
    assert read_mileage_km(fact_text(text)) == expected


PAPERS = [
    case("blue_card_english", "Документы: Blue Card на руках", "blue_card"),
    case("blue_card_one_word", "💰30.500.000₫|🛣~59 450км|📄Cavet 86-B2|BlueCard", "blue_card"),
    case("blue_card_in_russian", "Документы в порядке, Блю кард", "blue_card"),
    case("blue_card_spelled_blu", "есть блу карта и договор", "blue_card"),
    case("the_blue_card_phrase", "Оригинальная синяя карта на руках", "blue_card"),
    case("documents_in_order", "📄 Документы в порядке.", "blue_card"),
    case("documents_in_stock", "Документы в наличии", "blue_card"),
    case("with_documents", "с документами всё отлично", "blue_card"),
    case("with_documents_and_a_comma", "Продаю с документами, цена 5 млн", "blue_card"),
    case("technical_passport", "техпаспорт есть", "blue_card"),
    case("no_papers", "Без документов, на запчасти", "none"),
    case("no_blue_card", "no blue card, no papers", "none"),
    case("silence", "Honda Vision, 15 млн", None),
    case("both_is_a_conflict", "Без документов\nДокументы в порядке", None),
]


@pytest.mark.parametrize(("text", "expected"), PAPERS)
def test_papers_are_a_blue_card_none_or_not_said(text: str, expected: str | None) -> None:
    assert sale(text).get("papers") == expected


LICENSE = [
    case("not_needed", "50cc — права не нужны", True),
    case("not_required", "Для управления права не требуются!", True),
    case("licence_before", "Не нужны водительские права", True),
    case("licence_and_a_helmet", "Права и шлем не нужны!", True),
    case("licence_with_a_label", "Права: Не нужны (до 50 кубов)", True),
    case("licence_after_a_pronoun", "Права на него не нужны.", True),
    case("typo", "50 кубов, пава не нужны", True),
    case("without_a_licence", "Доступны без прав", True),
    case("english", "No license needed for 50cc", True),
    case("licence_is_needed", "Нужны права категории A", None),
    case("the_seller_has_none", "Продаю, нет прав категории А", None),
    case("a_ban_nearby", "Без прав ездить нельзя, штраф", None),
    case("silence", "Honda Vision, 15 млн", None),
]


@pytest.mark.parametrize(("text", "expected"), LICENSE)
def test_a_seller_claim_that_no_licence_is_needed(text: str, expected: bool | None) -> None:
    assert sale(text).get("no_license_claimed") is expected


BARGAIN = [
    case("negotiable_word", "Цена: 9.900.000 ₫, торг уместен", "negotiable"),
    case("negotiable_at_the_bike", "🤝 Торг возле скутера", "negotiable"),
    case("negotiable_with_a_clause", "небольшой торг при осмотре", "negotiable"),
    case("negotiable_english", "Price 20 million, negotiable", "negotiable"),
    case("fixed_without", "Цена 10 000 000 без торга", "fixed"),
    case("fixed_the_haggle_is_absent", "Много вложений, торга нет.", "fixed"),
    case("fixed_not_haggling", "По телефону не торгуюсь", "fixed"),
    case("fixed_price_phrase", "Цена фиксированная — 24 500 000", "fixed"),
    case("a_shopping_centre_is_not_a_haggle", "Торговый центр рядом", None),
    case("contradiction", "Без торга\nТорг уместен", None),
    case("silence", "Honda Vision, 15 млн", None),
]


@pytest.mark.parametrize(("text", "expected"), BARGAIN)
def test_a_bargain_is_negotiable_fixed_or_not_said(text: str, expected: str | None) -> None:
    assert sale(text).get("bargain") == expected


def test_a_rental_shop_describes_a_fleet_not_one_lot() -> None:
    """В прокате год, пробег и торг — чьи? Остаются только документы и «права не нужны»."""
    text = "АРЕНДА БАЙКОВ\nSYM Atilla 50cc 2022\nПробег 5000 км\nДоступны без прав\nТорг"

    assert bike_facts(fact_text(text), deal_type="rent_out") == {"no_license_claimed": True}


def test_a_full_sale_post_reads_every_fact_once() -> None:
    text = (
        "Продаю Honda Air Blade, 2012 год\n"
        "Пробег: 32 000 км\n"
        "Документы в порядке, Blue Card\n"
        "Права не нужны\n"
        "Цена 20 млн, торг\n"
    )

    assert sale(text) == {
        "year": 2012,
        "mileage_km": 32000,
        "papers": "blue_card",
        "no_license_claimed": True,
        "bargain": "negotiable",
    }
