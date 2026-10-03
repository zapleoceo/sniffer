"""Цена из текста объявления: что считается ценой и что нет.

Каждый случай — реальный шаблон из чатов (выборка 1900 текстов и разбор 18 868
карточек, 03.10.2026), сокращённый до строк, на которых он держится. Часть из них
— причина, по которой этот разбор вообще переписан: карточка «Oceanus» уходила в
базу «без цены», хотя в оригинале цена стояла строкой «Арендная плата: …».
"""

from __future__ import annotations

import time

import pytest

from sniffer.domain.prices import parse_price, parse_prices, price_hint

APARTMENT = {"category": "apartment", "deal_type": "rent_out"}
HOUSE = {"category": "house", "deal_type": "rent_out"}
BIKE_SALE = {"category": "motorbike", "deal_type": "sell"}
BIKE_RENT = {"category": "motorbike", "deal_type": "rent_out"}
ANY: dict[str, str] = {}


def case(
    name: str, text: str, kind: dict[str, str], amount: int, period: str | None = None
) -> object:
    return pytest.param(text, kind, amount, period, id=name)


LABELS = [
    case("label_and_unit", "Цена: 12 млн VND/месяц — 6 этаж", APARTMENT, 12_000_000, "month"),
    case(
        "stray_space_after_dot",
        "💵Цена: 15.000. 000 VND/месяц, залог за 1 месяц, предоплата за 1 месяц",
        APARTMENT,
        15_000_000,
        "month",
    ),
    case(
        "words_between_label_and_colon",
        "💰 Аренда при договоре на 3 месяца: 9 000 000 VND/месяц",
        APARTMENT,
        9_000_000,
        "month",
    ),
    case(
        "label_on_the_previous_line",
        "💰 СТОИМОСТЬ АРЕНДЫ:\n• 14.000.000 VND / месяц\n• 🔐 Залог: 1 месяц",
        APARTMENT,
        14_000_000,
        "month",
    ),
    case(
        "aggregator_footer_is_the_last_resort",
        "Сдам студию у моря, свободна с 1.10\n💰 9 000 000 ₫/мес #booking",
        APARTMENT,
        9_000_000,
        "month",
    ),
    case(
        "money_emoji_without_label", "🌿 Studio\n💵 14M VND/month", APARTMENT, 14_000_000, "month"
    ),
    case(
        "period_makes_a_bare_number_a_price", "➖7,500,000/month💸", APARTMENT, 7_500_000, "month"
    ),
    case("amount_alone_on_its_line", "Honda PCX 2018\n36 млн.", BIKE_SALE, 36_000_000),
    case(
        "payment_is_a_label",
        "Район Capella\nОплата 25.000.000\n2 оплаты, 2 депозита",
        HOUSE,
        25_000_000,
    ),
    case(
        "contract_terms_after_the_label",
        "💰 Цена 3-6-12 месяцев: 22 млн/месяц",
        APARTMENT,
        22_000_000,
        "month",
    ),
    case("index_before_the_price", "💰 2 — 13 млн/месяц", APARTMENT, 13_000_000, "month"),
    case("haggling_after_the_price", "Цена 10млн, торг", BIKE_SALE, 10_000_000),
    case(
        "sentence_boundary_ends_the_context", "Пробег 40 тыс км. Цена 15 млн", BIKE_SALE, 15_000_000
    ),
    case(
        "long_line_nearest_label_wins",
        "NVX 155 2017г байк, могу сделать скидку на это, пробег 21к цена 21млн",
        BIKE_SALE,
        21_000_000,
    ),
]

NOTATIONS = [
    case("lemon_means_million", "Продам Yamaha NVX 125cc 2017 год\n24 🍋", BIKE_SALE, 24_000_000),
    case("ml_with_a_currency", "💘11,5 ml vnd 💸\n💘 2 комнаты", APARTMENT, 11_500_000),
    case("integer_vnd_below_a_thousand", "💘18 vnd 💸\n💘 2 комнаты", APARTMENT, 18_000_000),
    case("decimal_vnd_below_a_thousand", "Свежий 19.5 vnd ❤️", BIKE_SALE, 19_500_000),
    case(
        "math_bold_digits",
        "#нячанг #аренда #сдам\nСтудия\n𝟙𝟚 𝟝𝟘𝟘 𝟘𝟘𝟘 𝕧𝕟𝕕\nЗалог: 1 месяц",
        APARTMENT,
        12_500_000,
    ),
    case(
        "keycap_digits_with_symbol_separators",
        "💵1️⃣5️⃣🔣0️⃣0️⃣0️⃣🔣0️⃣0️⃣0️⃣/мес.\n🔑 Депозит 1 мес. — оплата 1 мес.",
        APARTMENT,
        15_000_000,
        "month",
    ),
    case("cyrillic_o_for_zero", "💰 Цены:\n- 5ОО.ООО vnd /день", BIKE_RENT, 500_000, "day"),
    case("vietnamese_tr_fraction", "Cho thuê căn hộ giá 8tr5/tháng", APARTMENT, 8_500_000, "month"),
    case("vietnamese_tr_two_digits", "Giá: 16tr50 / tháng", APARTMENT, 16_500_000, "month"),
    case("vietnamese_tr_plain", "Giá 3.7tr", ANY, 3_700_000),
    case("d_for_dong", "Продаю. Поэтому 6 500 000 d. Торг при осмотре!!!", BIKE_SALE, 6_500_000),
    case("kk_means_million", "Цена 195кк (Донги или USDT)", BIKE_SALE, 195_000_000),
    case("bare_label_number_in_thousands", "Продам NVX, цена 19.800", BIKE_SALE, 19_800_000),
    case("vnd_sign_in_thousands", "💰 Цена: 6.500 ₫", BIKE_SALE, 6_500_000),
    case("bare_label_number_in_millions", "Цена 8.5", APARTMENT, 8_500_000),
]

LISTS = [
    case("range_takes_the_lower", "💵 Rent: 15-17 million/month", APARTMENT, 15_000_000, "month"),
    case(
        "flat_number_is_not_a_range",
        "🏢 Квартира №402 — 9 500 000 VND/месяц",
        APARTMENT,
        9_500_000,
        "month",
    ),
    case(
        "catalog_by_floors_takes_the_cheapest",
        "💰 Цены:\n🏢 2-й этаж — 11 000 000 донгов/месяц\n"
        "🏢 1-й этаж, внутренняя линия — 9 000 000 донгов/месяц",
        APARTMENT,
        9_000_000,
        "month",
    ),
    case(
        "ordinal_floor_is_not_the_price",
        "🧧 СТОИМОСТЬ АРЕНДЫ:\n🏢 4-й этаж: 13 МЛН/МЕСЯЦ\n🏢 3-й этаж: 12 МЛН/МЕСЯЦ",
        APARTMENT,
        12_000_000,
        "month",
    ),
    case(
        "bike_rent_prefers_the_monthly_price",
        "💰 Цены:\n📆 250.000 VND/сутки (от 3 дней)\n📅 от 2.000.000 VND/месяц\n🔐 Залог: 200$",
        BIKE_RENT,
        2_000_000,
        "month",
    ),
    case(
        "two_weeks_is_not_a_month",
        "Цена:\n200 000 VND / сутки\n1 800 000 VND /две недели\n2 500 000 VND / месяц",
        BIKE_RENT,
        2_500_000,
        "month",
    ),
]

CLAUSES = [
    case(
        "deposit_after_a_sentence_end",
        "Цена: 12,5 млн донгов/мес. Депозит за 2 месяца; оплата каждые 3 месяца.",
        APARTMENT,
        12_500_000,
        "month",
    ),
    case(
        "deposit_in_the_next_sentence",
        "Цена: 14 миллионов VND/месяц. Один депозит, один платеж",
        APARTMENT,
        14_000_000,
        "month",
    ),
    case(
        "deposit_field_after_a_dash",
        "💰 Цена: 19 000 000 VND - Залог: арендная плата за 1 месяц, Оплата: за 1 месяц",
        APARTMENT,
        19_000_000,
    ),
    case(
        "deposit_phrase_after_a_dash",
        "Цена 10,5 миллионов VND - Залог за 1 месяц, предоплата за 1 месяц",
        APARTMENT,
        10_500_000,
    ),
    case(
        "without_pets_does_not_name_the_amount",
        "🧧 Цена: 8 млн VND/месяц — без животных\n🐶🐈 С питомцами: +500 000 VND/месяц",
        APARTMENT,
        8_000_000,
        "month",
    ),
    case(
        "surcharge_is_not_the_price",
        "КВАРТИРА В ЦЕНТРЕ ЗА 12 МЛН\nКонтракт: от 1 мес(если менее 3 мес, то +1.5 млн)",
        ANY,
        12_000_000,
    ),
    case(
        "label_beats_a_label_with_words",
        "Цена: 3 млн, ИНЖЕКТОРНЫЙ\nпосторонний шум в вариаторе, цена ремонта 2 млн",
        BIKE_SALE,
        3_000_000,
    ),
]


@pytest.mark.parametrize(
    ("text", "kind", "amount", "period"), [*LABELS, *NOTATIONS, *LISTS, *CLAUSES]
)
def test_the_price_is_found_and_read_right(
    text: str, kind: dict[str, str], amount: int, period: str | None
) -> None:
    fact = parse_price(text, **kind)

    assert fact is not None, text
    assert (fact.amount, fact.period) == (amount, period)
    assert fact.currency == "VND"


def test_a_range_gives_the_lower_bound_and_keeps_the_upper() -> None:
    fact = parse_price("💵 Rent: 15-17 million/month", **APARTMENT)

    assert fact is not None
    assert (fact.amount, fact.up_to) == (15_000_000, 17_000_000)


def test_a_catalog_by_floors_keeps_the_dearest_as_the_upper_bound() -> None:
    text = "💰 Цены:\n🏢 2-й этаж — 11 000 000 донгов/месяц\n🏢 1-й этаж — 9 000 000 донгов/месяц"
    fact = parse_price(text, **APARTMENT)

    assert fact is not None
    assert (fact.amount, fact.up_to) == (9_000_000, 11_000_000)


NOT_A_PRICE = [
    pytest.param("Электричество: 5 000 VND / кВт·ч\nВода: 100 000 VND / чел / мес", id="utilities"),
    pytest.param("Залог: 10 млн\nДепозит 1 месяц", id="deposit"),
    pytest.param("🐶🐈 С питомцами: +500 000 VND/месяц", id="pet_surcharge"),
    pytest.param("✅ 275 000 донгов — интернет", id="amount_named_by_a_word_after_a_dash"),
    pytest.param("✅ 700,000 донгов — плата за обслуживание", id="amount_named_by_a_phrase"),
    pytest.param("➕ 500 000 VND/месяц за питомца", id="amount_named_after_the_period"),
    pytest.param("🧾 Управляющий сбор: 200 000 VND/месяц", id="management_fee"),
    pytest.param("🏍 Регистрация 550.000 VND", id="registration_fee"),
    pytest.param("Пробег 60 тыс км", id="mileage_in_thousands"),
    pytest.param("пробег 130к", id="mileage_with_cyrillic_k"),
    pytest.param("Пробег: 53 747 км", id="mileage"),
    pytest.param("Honda 125cc, 2021 год", id="engine_and_year"),
    pytest.param("Тел: 0905123456", id="phone_with_a_leading_zero"),
    pytest.param("WhatsApp +84 905 123 456", id="phone_with_a_country_code"),
    pytest.param("👉 АРЕНДА ДО 10 МЛН", id="link_to_a_selection"),
    pytest.param("📄 Срок аренды: 3 / 6 / 12 месяцев", id="contract_term"),
    pytest.param("🛡 Депозит 2 месяца, оплата 2", id="deposit_and_payment_in_months"),
    pytest.param("Цена: По запросу (1 комн.)", id="price_on_request"),
    pytest.param("Honda Lead 2010 год, код NHA-234, этаж 5", id="year_code_and_floor"),
    pytest.param("Масло 500 ml, воды 1 литр", id="milliliters"),
    pytest.param("📞 0905123456", id="phone_alone_on_its_line"),
    pytest.param(
        "🧧 СТОИМОСТЬ АРЕНДЫ:\n🏢 4-й этаж: по запросу", id="floor_number_under_a_price_header"
    ),
    pytest.param("☎️ 84905123456", id="phone_with_a_country_code_alone"),
    pytest.param("Цена: 21.500.000 млн VND", id="implausibly_large"),
]

# Суммы, которые разбор прочёл бы, но границы категории отбрасывают: без них
# «Регистрация 550.000» и «1,5 млн» вместо «16,5» стали бы арендой.
OUT_OF_BOUNDS = [
    pytest.param("Цена: 1.4 млн VND/мес (1 комн.)", id="below_the_apartment_floor"),
    pytest.param("Цена: 350 млн VND/мес (55 м²)", id="above_the_apartment_ceiling"),
    pytest.param("Сдам студию\n💰 500 000 ₫/мес #booking", id="aggregator_footer_gone_wrong"),
]


@pytest.mark.parametrize("text", [*NOT_A_PRICE, *OUT_OF_BOUNDS])
def test_things_that_are_not_the_price_are_not_read_as_one(text: str) -> None:
    assert parse_price(text, **APARTMENT) is None


@pytest.mark.parametrize("text", NOT_A_PRICE)
def test_the_price_hint_agrees_without_a_category(text: str) -> None:
    assert price_hint(text) == ("", None)


def test_the_rent_next_to_the_noise_is_still_found() -> None:
    text = (
        "Квартира у моря\n"
        "⚡ Электричество: 4 500 VND/кВтч\n"
        "💧 Вода: 150 000 VND/человек\n"
        "🧾 Управление: 100 000 VND/месяц\n"
        "🔐 Залог: 1 месяц\n"
        "💰 Цена: 13 млн VND/месяц\n"
        "📞 +84 905 123 456"
    )

    fact = parse_price(text, **APARTMENT)

    assert fact is not None
    assert fact.amount == 13_000_000


def test_a_bare_label_number_without_a_category_is_not_guessed() -> None:
    """Масштаб «19.500» выбирают границы категории; без категории выбирать нечем."""
    assert price_hint("Продам байк, цена 19.800") == ("", None)
    assert price_hint("Цена 8.5") == ("", None)


def test_the_text_that_the_price_hint_shows_keeps_the_label() -> None:
    assert price_hint("Цена 22 мил. Писать в личку") == ("Цена 22 мил.", 22_000_000)
    assert price_hint("Giá 3.7tr") == ("Giá 3.7tr", 3_700_000)
    assert price_hint("Honda PCX 2018\n36 млн.") == ("36 млн.", 36_000_000)


def test_a_price_in_dollars_is_kept_but_never_passed_off_as_dong() -> None:
    fact = parse_price("Rent: 2,000 USD", **HOUSE)

    assert fact is not None
    assert (fact.amount, fact.currency) == (2_000, "USD")
    assert price_hint("Rent: 2,000 USD") == ("", None)


def test_the_price_in_the_text_beats_the_aggregator_footer() -> None:
    """Бот-агрегатор вынимает цену наивно («200 тысяч» за шлемы вместо «10 млн» за байк)."""
    text = "Цена: 13 млн VND/месяц\n💰 12 000 000 ₫/мес #booking"

    fact = parse_price(text, **APARTMENT)

    assert fact is not None
    assert fact.amount == 13_000_000


def test_a_period_price_is_never_the_price_of_a_sale() -> None:
    """«Сдаётся за 6 млн/мес» в объявлении о продаже дома — доход соседнего, а не цена дома."""
    text = "Продам дом, соседний сдаётся за 6 млн/месяц"

    assert parse_price(text, deal_type="sell") is None


POISON_PILLS = {
    "digits_and_spaces": "1 " * 2000,
    "grouped_numbers": "1 000 " * 700,
    "keycap_spam": "1️⃣" * 1300,
    "one_endless_number": "9" * 4000,
    "ranges": "10-20-30-40-50-" * 280,
    "labels_with_numbers": "цена 5 " * 600,
}


@pytest.mark.parametrize("text", POISON_PILLS.values(), ids=POISON_PILLS.keys())
def test_a_hostile_text_neither_crashes_nor_stalls_the_funnel(text: str) -> None:
    """Воронка обрабатывает сообщения по одному: одно кривое не смеет её остановить.

    Прогон 4-килобайтных текстов 03.10.2026 нашёл оба дефекта: строка из тысячи
    чисел считалась по четыре секунды (контекст каждой суммы резал всю строку), а
    число из четырёх тысяч девяток давало `OverflowError` на `int(inf)`.
    """
    started = time.perf_counter()

    parse_prices(text)
    parse_price(text, **APARTMENT)

    assert time.perf_counter() - started < 1.5


def test_the_limits_that_keep_the_funnel_fast_are_part_of_the_contract() -> None:
    """Предел цены контекста — часть договора, а не случайность реализации.

    Окно слева 200 знаков (метка дальше окна метка не считается), 60 сумм на
    строку, 6000 знаков на текст: объявление, у которого цена стоит после них, —
    не объявление, а простыня, и читать её дальше значит платить временем воронки.
    """
    far_label = "цена" + " " * 300 + "12 млн"
    crowded_line = "1 " * 70 + "цена 12 млн"
    long_text = "x" * 6_100 + "\nЦена: 12 млн"

    near = parse_price("Цена: 12 млн", **APARTMENT)
    far = parse_price(far_label, **APARTMENT)

    assert near is not None
    assert near.source == "label"
    assert far is not None
    assert far.source == "text"
    assert parse_price(crowded_line, **APARTMENT) is None
    assert parse_price(long_text, **APARTMENT) is None
