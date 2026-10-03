"""Находки ревью разбора цены (Opus, 03.10.2026): каждая — отдельный живой шаблон.

Тексты сокращены до строк, на которых держится ошибка. Пока тест красный, ошибка
воспроизводится; зелёный — исправление работает и соседние случаи целы.
"""

from __future__ import annotations

import pytest

from sniffer.domain.prices import parse_price, parse_prices, price_hint

APARTMENT = {"category": "apartment", "deal_type": "rent_out"}
HOUSE = {"category": "house", "deal_type": "rent_out"}
BIKE_SALE = {"category": "motorbike", "deal_type": "sell"}
BIKE_RENT = {"category": "motorbike", "deal_type": "rent_out"}


def case(
    name: str, text: str, kind: dict[str, str], amount: int, period: str | None = None
) -> object:
    return pytest.param(text, kind, amount, period, id=name)


# Голое число без единицы и валюты становилось миллионами под заголовком или
# значком денег и выигрывало как «наименьшая сумма»: этаж, срок, число шлемов.
BARE_NUMBERS = [
    case(
        "floor_numbers_under_a_price_header",
        "💵 Цена аренды:\n• 2, 3 этаж — фасад: 12.500.000 VND/месяц\n"
        "• 4, 5 этаж: 13.500.000 VND/месяц",
        APARTMENT,
        12_500_000,
        "month",
    ),
    case(
        "contract_term_range",
        "💵 Rent: 10,000,000 VND/month\n📝 Flexible rental terms:\n✅ 3 - 6 month contract",
        APARTMENT,
        10_000_000,
        "month",
    ),
    case(
        "months_after_a_money_sign",
        "💰 20,000,000 VND/month\n💵 2-month deposit – 2-month payment cycle",
        HOUSE,
        20_000_000,
        "month",
    ),
    case(
        "items_under_an_included_header",
        "⭐ 5 000 000 VND / месяц\n\nВ стоимость включено:\n🪖 2 качественных шлема",
        BIKE_RENT,
        5_000_000,
        "month",
    ),
    case(
        "rooms_under_a_rent_header",
        "Дом в аренду в районе Мипеко:\nГостиная, 4 спальни, 5 ванных (2 с ваннами)\n"
        "Стоимость аренды: 38 миллионов VND/месяц",
        HOUSE,
        38_000_000,
        "month",
    ),
    case(
        "deal_word_after_a_list_number",
        "💰 Цены:\n17,8 покупка\n2,8/ мес. аренда",
        BIKE_SALE,
        17_800_000,
    ),
]


@pytest.mark.parametrize(("text", "kind", "amount", "period"), BARE_NUMBERS)
def test_a_bare_number_is_not_taken_for_millions(
    text: str, kind: dict[str, str], amount: int, period: str | None
) -> None:
    fact = parse_price(text, **kind)

    assert fact is not None, text
    assert (fact.amount, fact.period) == (amount, period)


def test_a_dollar_abbreviation_with_a_dot_is_a_currency_not_a_dong_amount() -> None:
    """«Цена 180 дол.» читалась как 180 000 000 донгов: «дол.» не знали валютой."""
    text = "Продам скутер SYM Excell. Цена 180 дол. С документами"

    assert [(fact.amount, fact.currency) for fact in parse_prices(text)] == [(180, "USD")]
    assert price_hint(text) == ("", None)


# Слово-метка в чужом смысле выигрывало у настоящей цены: «наименьшая из меток».
OTHER_SENSE = [
    case(
        "part_of_the_item_has_its_own_cost",
        "Прошёл ТО стоимостью 4,2 млн. Всё заменено.\nЦена: 39 млн.",
        BIKE_SALE,
        39_000_000,
    ),
    case(
        "daily_rent_next_to_the_monthly_price",
        "🆓Студия 35м2: 13 млн донгов\n🔰посуточная аренда: 2.2 млн донгов (всё включено)",
        APARTMENT,
        13_000_000,
    ),
    case(
        "cleaning_payment_is_a_fee",
        "КВАРТИРА У РЕКИ ЗА 15 МЛН\nМожно с животными (оплата клининг при выезде 2млн)",
        APARTMENT,
        15_000_000,
    ),
    case(
        "tenant_is_a_person_not_a_label",
        "🔥 АРЕНДА СТУДИИ | 5,5 МЛН VND/МЕСЯЦ\n🌍 1 иностранный арендатор — 6 млн VND/месяц",
        APARTMENT,
        5_500_000,
        "month",
    ),
    case(
        "field_label_after_other_fields",
        "Bán Nhà Hẻm. DT: 48m2 - Giá: 4,5tỷ\nChủ hạ giá từ 4 tỷ",
        {"category": "house", "deal_type": "sell"},
        4_500_000_000,
    ),
    case(
        "rental_period_is_not_a_price",
        "6️⃣ Price for 1 month: 14,5 million\n7️⃣ Minimum rental period: #3-6 m",
        APARTMENT,
        14_500_000,
        "month",
    ),
]


@pytest.mark.parametrize(("text", "kind", "amount", "period"), OTHER_SENSE)
def test_a_label_word_in_another_sense_does_not_win(
    text: str, kind: dict[str, str], amount: int, period: str | None
) -> None:
    fact = parse_price(text, **kind)

    assert fact is not None, text
    assert (fact.amount, fact.period) == (amount, period)


AGGREGATOR_LINES = [
    pytest.param("Сдам студию у моря\n💰 9 000 000 ₫/мес #booking", id="alone"),
    pytest.param(
        "🟡 Условия аренды:\n• Цена: уточняйте\n👉 АРЕНДА ДО 10 МЛН\n💰 10 000 000 ₫ #booking",
        id="repeats_a_ceiling_of_a_selection",
    ),
    pytest.param(
        "💰 Депозит — 2 месяца, оплата — 1 месяц\n💰 2 000 ₫/мес #booking", id="repeats_months"
    ),
    pytest.param(
        "📐 Land area: 72m² | Total usable area: 216m²\n💰 72 200 000 ₫ #booking",
        id="repeats_an_area",
    ),
]


@pytest.mark.parametrize("text", AGGREGATOR_LINES)
def test_the_aggregator_summary_line_is_never_read_as_a_price(text: str) -> None:
    """Бот-агрегатор вынимает цену наивно: то, что разбор отверг, он возвращал бы обратно."""
    assert parse_prices(text) == []
    assert parse_price(text, **APARTMENT) is None
    assert parse_price(text, **HOUSE) is None


def test_a_word_that_merely_starts_like_a_period_is_not_a_period() -> None:
    """«Место осмотра» делало цену продажи месячной («мес») — а продаже месяц не идёт."""
    text = "Honda Lead, вложений не требует. ЦЕНА 9 млн. Место осмотра по договорённости"
    fact = parse_price(text, **BIKE_SALE)

    assert fact is not None
    assert (fact.amount, fact.period) == (9_000_000, None)
    assert parse_price("Цена 9 млн деньги вперёд, годится", **BIKE_SALE) is not None


FEATURES_NOT_FEES = [
    case("without_a_fee", "Сдаю квартиру без комиссии 12 млн", APARTMENT, 12_000_000),
    case("with_parking", "Квартира с парковкой 15 млн в месяц", APARTMENT, 15_000_000, "month"),
    case("pets_allowed", "Можно с животными 12 млн/мес", APARTMENT, 12_000_000, "month"),
    case("electric_scooter", "Продам электроскутер VinFast 12 млн", BIKE_SALE, 12_000_000),
    case(
        "electric_scooter_in_english",
        "Electric scooter for sale 9,000,000 VND",
        BIKE_SALE,
        9_000_000,
    ),
    case(
        "including_internet",
        "Квартира, включая интернет 12 млн/мес",
        APARTMENT,
        12_000_000,
        "month",
    ),
    case("price_then_a_feature", "Студия 12 млн/мес с парковкой", APARTMENT, 12_000_000, "month"),
]


@pytest.mark.parametrize(("text", "kind", "amount", "period"), FEATURES_NOT_FEES)
def test_a_feature_of_the_item_next_to_the_price_does_not_kill_it(
    text: str, kind: dict[str, str], amount: int, period: str | None
) -> None:
    """Слово «за что платят» называет сумму только вплотную и без «с», «без», «включая»."""
    fact = parse_price(text, **kind)

    assert fact is not None, text
    assert (fact.amount, fact.period) == (amount, period)


def test_a_fee_stays_a_fee_next_to_the_words_that_name_it() -> None:
    assert parse_price("Парковка 3 000 000 VND/мес", **HOUSE) is None
    assert parse_price("Залог за квартиру: 20 млн", **APARTMENT) is None
    assert parse_price("Комиссия агентства 15 млн", **APARTMENT) is None


RANGES = [
    case("dash_without_spaces", "Цены: 3–10 млн VND/месяц", APARTMENT, 3_000_000, "month"),
    case("from_to_words", "Студии от 4 до 15 млн в месяц", APARTMENT, 4_000_000, "month"),
    case("english_to", "Rent: 5 to 40 million/month", APARTMENT, 5_000_000, "month"),
    case("area_before_the_dash", "Студия 35 м2 — 12 млн/месяц", APARTMENT, 12_000_000, "month"),
    case("number_before_the_dash", "ДОМ ХА КУАНГ 2 – 35 МЛН/МЕСЯЦ", HOUSE, 35_000_000, "month"),
    case("model_before_the_dash", "Yamaha NVX 125 — 17.000.000₫", BIKE_SALE, 17_000_000),
]


@pytest.mark.parametrize(("text", "kind", "amount", "period"), RANGES)
def test_a_range_is_a_range_and_an_index_before_a_dash_is_not(
    text: str, kind: dict[str, str], amount: int, period: str | None
) -> None:
    fact = parse_price(text, **kind)

    assert fact is not None, text
    assert (fact.amount, fact.period) == (amount, period)


def test_a_wide_range_keeps_its_upper_bound() -> None:
    fact = parse_price("Студии от 4 до 15 млн в месяц", **APARTMENT)

    assert fact is not None
    assert fact.up_to == 15_000_000


def test_a_hashtag_is_a_tag_not_an_amount() -> None:
    """«#от10до15млн» — корзина фильтра агрегатора, а не цена объявления за 12 млн."""
    text = "Студия 12 млн/мес #нячанг #студия #от10до15млн\nМинимальный срок: #3-6 m"
    fact = parse_price(text, **APARTMENT)

    assert fact is not None
    assert (fact.amount, fact.period) == (12_000_000, "month")
    assert [item.amount for item in parse_prices(text)] == [12_000_000]


# Живой Telegram-поиск видит только текст: стороны сделки у него нет.
LIVE = [
    pytest.param(
        "Продам байк 18.5 млн, можно в аренду 200к/сутки", 18_500_000, id="sale_with_a_daily_rent"
    ),
    pytest.param("Сдам виллу, 5 млн/ночь", None, id="daily_price_is_not_price_vnd"),
    pytest.param("Аренда байка 1,5 млн/неделя", None, id="weekly_price_is_not_price_vnd"),
    pytest.param("Сдам квартиру 10 млн/мес, залог 10 млн", 10_000_000, id="rent_and_a_deposit"),
    pytest.param(
        "Honda Vision 150к/сутки, 2.5 млн/месяц", 2_500_000, id="bike_with_a_monthly_price"
    ),
    pytest.param(
        "Bán căn hộ 4,39 tỷ, có HĐ thuê 13 triệu/tháng", None, id="sale_and_rent_cannot_be_told"
    ),
]


@pytest.mark.parametrize(("text", "price"), LIVE)
def test_the_live_hint_never_passes_off_a_rate_as_the_price(text: str, price: int | None) -> None:
    """Без стороны сделки `price_vnd` — разовая или месячная цена; суточная ей не бывает."""
    assert price_hint(text)[1] == price


@pytest.mark.parametrize(
    ("text", "amount"),
    [
        ("Цена 2.05 млн", 2_050_000),
        ("Цена 2.01 млн", 2_010_000),
        ("Цена 8,07 млн/мес", 8_070_000),
        ("Цена 15.65 млн", 15_650_000),
        ("Цена: 4,35 tỷ", 4_350_000_000),
        ("Цена: 19.8 млн", 19_800_000),
    ],
)
def test_a_decimal_amount_is_rounded_not_truncated(text: str, amount: int) -> None:
    """В числах с плавающей точкой «2.05 млн» — 2049999.9999999998: усечение теряло донг."""
    (fact,) = parse_prices(text)

    assert fact.amount == amount


COMPOUND = [
    pytest.param("Giá bán: 2 tỷ 300 triệu", 2_300_000_000, id="vietnamese_billions_and_millions"),
    pytest.param("Giá: 4 tỷ 850 triệu", 4_850_000_000, id="vietnamese_another"),
    pytest.param("Цена: 4 миллиона 500 тысяч", 4_500_000, id="russian_with_units"),
    pytest.param("Цена: 4 миллиона 500", 4_500_000, id="russian_the_next_rank_is_understood"),
    pytest.param("Цена: 2 млрд 300 млн", 2_300_000_000, id="billions_and_millions"),
    pytest.param("Цена 9 млн 500к", 9_500_000, id="millions_and_k"),
    pytest.param("Цена 15 млн 500 м от моря", 15_000_000, id="metres_are_not_a_tail"),
    pytest.param("Цена 15 млн 2 комнаты", 15_000_000, id="rooms_are_not_a_tail"),
    pytest.param("Цена 9 млн 905 123 456", 9_000_000, id="a_phone_is_not_a_tail"),
    pytest.param("Цена 2 млн 300 млн донгов", 2_000_000, id="a_tail_of_the_same_rank_is_ignored"),
]


@pytest.mark.parametrize(("text", "amount"), COMPOUND)
def test_a_sum_written_in_two_parts_is_read_whole(text: str, amount: int) -> None:
    """«2 tỷ 300 triệu» читалось как две суммы, и побеждала меньшая — 300 миллионов."""
    (fact,) = parse_prices(text)

    assert fact.amount == amount


def test_a_tail_that_is_not_part_of_the_sum_stays_out_of_the_shown_text() -> None:
    (fact,) = parse_prices("Цена 15 млн 2 комнаты")

    assert fact.raw == "Цена 15 млн"


@pytest.mark.parametrize(
    ("text", "amount"),
    [
        ("Giá 2 tỉ", 2_000_000_000),
        ("Giá 2 tỉ 300 triệu", 2_300_000_000),
        ("Price 15 mil", 15_000_000),
        ("Rent 15ml/month", 15_000_000),
    ],
)
def test_the_spellings_the_review_found_missing_are_read(text: str, amount: int) -> None:
    (fact,) = parse_prices(text)

    assert fact.amount == amount


@pytest.mark.parametrize(
    ("text", "amount"),
    [
        ("Цена 15 млн 50 метров до моря", 15_000_000),
        ("Цена 150 тыс 500", 150_000),
        ("Цена 15 млн 45 метров до моря", 15_000_000),
    ],
)
def test_a_number_after_a_unit_is_a_tail_only_when_it_can_be_one(text: str, amount: int) -> None:
    """Хвост без единицы — три круглые цифры после миллионов, не «50 метров»."""
    (fact,) = parse_prices(text)

    assert fact.amount == amount


def test_the_upper_end_of_a_range_is_rounded_like_the_lower() -> None:
    (fact,) = parse_prices("Цена: 1.9–2.05 млн")

    assert (fact.amount, fact.up_to) == (1_900_000, 2_050_000)
