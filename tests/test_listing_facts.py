"""Факты карточки: как они ложатся в колонки и поверх явных атрибутов разбора запроса.

Чтения фактов проверены по отдельности (`test_facts_*`, `test_listing_title`,
`test_text_lang`); здесь — склейка и то, что с ней связано: кто главнее, чего в карточке
быть не может, и что `listing_from` ничего не меняет в чужих полях.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from sniffer.domain.records import Chat, RawMessage
from sniffer.pipeline.archive import classify, listing_from
from sniffer.pipeline.listing_facts import deposit_in_months, fact_columns
from sniffer.search import vocabulary
from sniffer.search.intake_rules import parse_query

HOUSING = (
    "Сдаётся 2-комнатная квартира в Oceanus, 5 этаж\n"
    "📐 Площадь: 65 м2\n"
    "Балкон, лифт, бассейн. Без животных.\n"
    "Депозит 1 месяц, контракт от 3 месяцев\n"
    "5 минут до моря\n"
    "Цена: 18 млн VND/месяц\n"
)


def test_a_housing_post_fills_the_facts_the_passport_promises() -> None:
    columns = fact_columns(HOUSING, category="apartment", deal_type="rent_out")

    assert columns.district == "oceanus"
    assert columns.lang == "ru"
    assert columns.title == "Сдаётся 2-комнатная квартира в Oceanus, 5 этаж"
    assert columns.attributes == {
        "zone": "north",
        "balcony": True,
        "elevator": True,
        "pool": True,
        "pets_allowed": False,
        "sea_distance_min": 5,
        "area_m2": 65,
        "floor": 5,
        "deposit_months": 1,
        "min_term_months": 3,
    }


def test_a_house_is_read_by_its_storeys_not_by_a_floor() -> None:
    columns = fact_columns("Дом 3 этажа, 4 спальни, 130 м2", category="house", deal_type="rent_out")

    assert columns.attributes == {"floors_total": 3, "area_m2": 130}


def test_a_bike_post_is_read_by_the_bike_facts_and_not_by_the_housing_ones() -> None:
    text = "Honda Vision 2019\nПробег 15 000 км\nБлю кард есть, права не нужны, торг\nБалкон лифт"

    columns = fact_columns(text, category="motorbike", deal_type="sell")

    assert columns.attributes == {
        "year": 2019,
        "mileage_km": 15000,
        "papers": "blue_card",
        "no_license_claimed": True,
        "bargain": "negotiable",
    }


def test_a_rented_flat_reads_the_lease_and_a_sold_one_does_not() -> None:
    """«Контракт» в продаже квартиры — не срок аренды, а залога у продажи нет."""
    text = "Продаю квартиру, 40 м2, контракт от 3 месяцев, депозит 1 месяц"

    columns = fact_columns(text, category="apartment", deal_type="sell")

    assert columns.attributes == {"area_m2": 40}


def test_other_categories_get_only_the_place_the_language_and_the_title() -> None:
    columns = fact_columns(
        "Продам велосипед, Фыок Лонг, 3 млн", category="bicycle", deal_type="sell"
    )

    assert (columns.district, columns.lang) == ("phuoc_long", "ru")
    assert columns.attributes == {"zone": "south"}


def test_an_unknown_category_does_not_fail_the_funnel() -> None:
    assert fact_columns("Что-то продаю", category="spaceship", deal_type="sell").title


def test_what_the_request_parser_already_knows_wins_over_what_is_read_here() -> None:
    text = "Квартира, 2 спальни, полностью меблирована, балкон"
    known = {"rooms": 3, "furnished": False}

    columns = fact_columns(text, category="apartment", deal_type="rent_out", attributes=known)

    assert columns.attributes["rooms"] == 3
    assert columns.attributes["furnished"] is False
    assert columns.attributes["balcony"] is True


@pytest.mark.parametrize("empty", [None, "", [], {}])
def test_an_empty_known_value_does_not_hide_a_fact(empty: object) -> None:
    """Пустое значение разбора — «не нашёл», а не «нашёл, что пусто»."""
    text = "Honda Vision, блю кард есть"

    columns = fact_columns(
        text, category="motorbike", deal_type="sell", attributes={"papers": empty}
    )

    assert columns.attributes["papers"] == "blue_card"


def test_a_false_flag_of_the_request_parser_is_a_value_and_stays() -> None:
    text = "Honda Vision, 2019"

    columns = fact_columns(
        text, category="motorbike", deal_type="sell", attributes={"power": False}
    )

    assert columns.attributes["power"] is False


def test_a_bike_is_titled_by_its_make_when_the_post_has_no_title() -> None:
    columns = fact_columns(
        "#motorbikes\n🔥🔥",
        category="motorbike",
        deal_type="sell",
        attributes={"brand": "honda", "model": "vision"},
    )

    assert columns.title == "Honda Vision"


def test_the_facts_of_a_flat_never_contain_a_contact() -> None:
    """Телефон, @username и Zalo в карточку не попадают: в ней ссылка на оригинал."""
    text = (
        "Сдаётся студия у моря, 30 м2\n"
        "Звоните +84 900 000 000, Zalo 0900 000 000\n"
        "Telegram: @example_agent, https://t.me/example_agent\n"
        "Депозит 1 месяц, контракт от 3 месяцев\n"
    )

    columns = fact_columns(text, category="apartment", deal_type="rent_out")
    stored = json.dumps([columns.title, columns.district, columns.lang, columns.attributes])

    for contact in ("+84", "900 000", "0900", "example_agent", "t.me", "Zalo", "@"):
        assert contact not in stored


# ── сквозь `listing_from`: так карточка рождается в воронке ────────────────────────────

DETECTOR = vocabulary.category_hints


def raw(text: str) -> RawMessage:
    return RawMessage(
        id=7,
        chat_tg_id=-1001234567890,
        msg_id=88,
        text=text,
        text_hash="hash",
        posted_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


@pytest.fixture
def chat() -> Chat:
    return Chat(tg_id=-1001234567890, username="nha_trang_flea", title="Flea", city="nha_trang")


def card(text: str, chat: Chat):  # type: ignore[no-untyped-def]
    message = raw(text)
    parsed = parse_query(message.text, default_city=chat.city)
    return listing_from(
        message,
        chat,
        classify(message, category_hints=DETECTOR),
        deal_type="rent_out",
        attributes=dict(parsed.attributes),
        city=parsed.city or "",
    )


def test_the_funnel_builds_the_card_with_the_facts_of_the_post(chat: Chat) -> None:
    listing = card(HOUSING, chat)

    assert listing.district == "oceanus"
    assert listing.lang == "ru"
    assert listing.title == "Сдаётся 2-комнатная квартира в Oceanus, 5 этаж"
    assert listing.attributes["area_m2"] == 65
    assert listing.attributes["zone"] == "north"
    assert listing.attributes["rooms"] == 2  # прочитано разбором запроса и не перезаписано


def test_the_title_of_a_card_is_no_longer_the_agency_brand(chat: Chat) -> None:
    text = "AN-HOME\nаренда начинается здесь…\n\n🌿 Дом с 5 спальнями в Центре Нячанга 🌴\nЦена: 35 млн VND/месяц"  # noqa: E501

    assert card(text, chat).title == "Дом с 5 спальнями в Центре Нячанга"


def test_the_price_attributes_still_win_over_the_facts(chat: Chat) -> None:
    """Суточная цена кладётся в `rate_*`, и факт с тем же именем ей не мешает."""
    listing = card("Студия в аренду, 35 м2, 5 млн VND/день, Phuoc Long", chat)

    assert "rate_amount" in listing.attributes
    assert listing.attributes["area_m2"] == 35


def test_a_danang_flat_in_a_nha_trang_chat_is_a_danang_card(chat: Chat) -> None:
    """Решение владельца 04.10.2026: город называет сам пост, а не чат, где он лежит."""
    text = "🔥 STUDIO APARTMENT FOR RENT – HAI CHAU DISTRICT\n💰 Price: 9.5M VND/month"

    listing = card(text, chat)

    assert listing.city == "da_nang"
    assert listing.district == "hai_chau"
    assert listing.lang == "en"
    assert "zone" not in listing.attributes


def test_a_nha_trang_flat_keeps_its_city_and_a_post_without_a_place_gets_the_chat_one(
    chat: Chat,
) -> None:
    assert card("Сдаётся квартира в Oceanus, 15 млн VND/месяц", chat).city == "nha_trang"
    assert card("Сдаётся квартира у моря, 15 млн VND/месяц", chat).city == "nha_trang"


def test_a_hanoi_post_is_never_turned_into_danang_by_the_place_book(chat: Chat) -> None:
    message = raw("Сдаётся квартира в Hai Chau, 15 млн VND/месяц")
    hanoi = listing_from(
        message,
        chat,
        classify(message, category_hints=DETECTOR),
        deal_type="rent_out",
        city="ha_noi",
    )

    assert hanoi.city == "ha_noi"


def test_a_deposit_named_by_a_sum_becomes_months_through_the_monthly_rent(chat: Chat) -> None:
    text = "Сдаётся квартира в Oceanus\nДепозит 18 млн\nЦена: 9 млн VND/месяц"

    listing = card(text, chat)

    assert listing.attributes["deposit_amount"] == 18_000_000
    assert listing.attributes["deposit_months"] == 2


def test_the_months_named_in_the_post_win_over_the_recalculated_ones() -> None:
    columns = fact_columns(
        "Депозит 1 месяц, залог 18 млн",
        category="apartment",
        deal_type="rent_out",
        monthly_rent=9_000_000,
    )

    assert columns.attributes["deposit_months"] == 1
    assert columns.attributes["deposit_amount"] == 18_000_000


@pytest.mark.parametrize(
    ("amount", "rent", "expected"),
    [
        (18_000_000, 9_000_000, 2),
        (13_500_000, 9_000_000, 1.5),
        (4_500_000, 9_000_000, 0.5),
        (9_000_000, None, None),
        (9_000_000, 0, None),
        (900_000_000, 9_000_000, None),
        (1_000_000, 9_000_000, None),
        (63_000_000, 9_000_000, None),
        (54_000_000, 9_000_000, 6),
        ("18", 9_000_000, None),
        (True, 9_000_000, None),
    ],
)
def test_a_deposit_sum_in_months_is_plausible_or_nothing(
    amount: object, rent: int | None, expected: float | None
) -> None:
    result = deposit_in_months(amount, rent)

    assert result == expected
    if isinstance(expected, int):
        assert isinstance(result, int), "целое число месяцев не должно храниться как 2.0"


def test_a_city_without_a_reference_gets_no_nha_trang_zone() -> None:
    """«В центре города» в ханойском объявлении — не «Центр Нячанга»."""
    text = "Сдаётся квартира в центре города, Phuoc Hai"

    hanoi = fact_columns(text, category="apartment", deal_type="rent_out", city="ha_noi")
    nha_trang = fact_columns(text, category="apartment", deal_type="rent_out", city="nha_trang")
    default = fact_columns(text, category="apartment", deal_type="rent_out")

    assert (hanoi.district, "zone" in hanoi.attributes) == (None, False)
    assert (nha_trang.district, nha_trang.attributes["zone"]) == ("phuoc_hai", "center")
    assert default == nha_trang


def test_the_funnel_passes_the_city_of_the_card_to_the_facts(chat: Chat) -> None:
    """Город карточки (из текста лота) решает, есть ли у неё схема зон Нячанга."""
    message = raw("Сдаётся квартира 2 спальни в центре города, 15 млн VND/месяц")

    hanoi = listing_from(
        message,
        chat,
        classify(message, category_hints=DETECTOR),
        deal_type="rent_out",
        city="ha_noi",
    )

    assert hanoi.district is None
    assert "zone" not in hanoi.attributes


def test_a_post_without_a_headline_is_titled_by_its_facts_and_its_district() -> None:
    """Меню-карточка «СДАМ КВАРТИРУ / Студия / …» не имеет названия — берём из фактов."""
    text = "СДАМ КВАРТИРУ\nСтудия\n📍 Район: Phuoc Long\n15 500 000 vnd\nЗалог: 1 месяц\n"

    columns = fact_columns(text, category="apartment", deal_type="rent_out")

    assert columns.title == "Студия · Phước Long"
    assert columns.district == "phuoc_long"


def test_the_district_is_stored_as_a_slug_and_shown_by_its_name() -> None:
    columns = fact_columns(
        "СДАМ\nНЯЧАНГ ЮГ\n9 000 000 vnd", category="apartment", deal_type="rent_out"
    )

    assert columns.district is None
    assert columns.title == "Квартира · Юг Нячанга"


def test_an_explicit_attribute_beats_the_one_read_from_the_text() -> None:
    text = "Сдаётся квартира, 5 этаж\nПлощадь: 65 м2"

    columns = fact_columns(
        text, category="apartment", deal_type="rent_out", attributes={"floor": 3, "rooms": 2}
    )

    assert columns.attributes["floor"] == 3
    assert columns.attributes["area_m2"] == 65
    assert columns.attributes["rooms"] == 2
