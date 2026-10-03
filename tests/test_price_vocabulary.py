"""Каждое слово словаря письма держится примером: убери его — и тест покраснеет.

Опус при ревью 03.10.2026 ломал словарь по одному слову, и в 47 случаях все тесты
оставались зелёными: слово можно было вычеркнуть, и никто бы не заметил. Здесь —
по примеру на каждое такое слово. Примеры нарочно парные: у настоящей цены есть
«соперник» поменьше, и только правильное слово ставит её выше него. Один факт на
текст такого не проверяет: он выигрывает при любой метке.
"""

from __future__ import annotations

import pytest

from sniffer.domain.prices import parse_price, parse_prices

APARTMENT = {"category": "apartment", "deal_type": "rent_out"}
BIKE_SALE = {"category": "motorbike", "deal_type": "sell"}
BIKE_RENT = {"category": "motorbike", "deal_type": "rent_out"}
HOUSE = {"category": "house", "deal_type": "rent_out"}
# Сторона известна, а категории нет: границ правдоподобия нет, срок «в месяц» разрешён.
RENT = {"deal_type": "rent_out"}

# Соперник без метки: 300 тысяч за шлем. Метка обязана поставить цену выше него.
TEXT_RIVAL = "Шлем в подарок 300 000 ₫\n"
# Соперник с меткой и словами («цена ремонта»): слабая метка, её перебивает только сильная.
WEAK_RIVAL = "Цена ремонта 3 млн\n"


def price_of(text: str, **kind: str) -> int | None:
    fact = parse_price(text, **kind)
    return None if fact is None else fact.amount


@pytest.mark.parametrize(
    "word",
    ["Аренда", "Стоимость", "Цена", "Оплата", "Price", "Rent", "Cost", "Giá", "Giá thuê"],
)
def test_every_label_word_makes_a_sum_a_price_over_a_bare_rival(word: str) -> None:
    assert price_of(f"{TEXT_RIVAL}{word} 12 млн") == 12_000_000


@pytest.mark.parametrize(
    "word", ["Аренда", "Стоимость", "Цена", "Price", "Rent", "Cost", "Giá thuê"]
)
def test_a_label_with_a_colon_beats_a_label_with_words(word: str) -> None:
    assert price_of(f"{WEAK_RIVAL}{word}: 12 млн") == 12_000_000


def test_a_phrase_of_words_before_the_colon_still_makes_a_label() -> None:
    text = f"{WEAK_RIVAL}Аренда при договоре на 3 месяца: 9 000 000 VND/месяц"

    assert price_of(text, **RENT) == 9_000_000


@pytest.mark.parametrize(
    "text",
    [
        "аренда студии в центре 12 млн",
        "цена ремонта 12 млн",
        "rent for the studio 12 млн",
    ],
)
def test_a_label_and_a_few_words_still_beat_a_bare_sum(text: str) -> None:
    """До трёх слов между меткой и суммой: «аренда Нячанг 14.5 млн» — такая же цена."""
    assert price_of(f"{TEXT_RIVAL}{text}") == 12_000_000


@pytest.mark.parametrize("sign", list("💰💵💸💲🪙🤑💴💶💷"))
def test_every_money_sign_makes_a_sum_a_price_over_a_bare_rival(sign: str) -> None:
    assert price_of(f"{TEXT_RIVAL}{sign} 15 млн") == 15_000_000


@pytest.mark.parametrize(
    ("stop", "name"),
    [(", ", "comma"), (". ", "dot"), ("! ", "exclamation"), ("? ", "question")],
)
def test_a_sentence_boundary_starts_a_new_clause_for_a_label(stop: str, name: str) -> None:
    """Метка в начале фразы сильнее метки посреди фразы — а фразу начинает знак препинания."""
    text = f"Honda Vision 2019 года{stop}цена 21 млн{stop}цена ремонта 3 млн"

    assert price_of(text) == 21_000_000, name


def test_a_header_over_a_list_hands_its_label_to_the_lines() -> None:
    text = f"{TEXT_RIVAL}💰 Цены:\n• 9 000 000 VND/мес"

    assert price_of(text, **RENT) == 9_000_000


def test_a_line_without_a_colon_is_not_a_header() -> None:
    assert price_of("КВАРТИРА В АРЕНДУ\n8.5") is None


def test_a_header_with_digits_hands_nothing_to_the_lines() -> None:
    assert price_of("🟡 Условия аренды №3:\n8.5") is None


# Слово из каждой ветки `_PAID_FOR`: сумма при нём — сбор, а не цена предмета. Без
# категории границ правдоподобия нет, так что отвергает сумму только само слово.
FEE_WORDS = [
    "залог", "депозит", "deposit", "cọc", "вода", "воду", "water", "nước",
    "электричество", "электроэнергия", "э/э", "electricity", "electric bill", "điện",
    "управление", "менеджмент", "management", "обслуживание", "сервис", "service",
    "интернет", "internet", "wifi", "wi-fi", "вывоз мусора", "мусор", "trash", "garbage",
    "парковка", "parking", "уборка", "клининг", "cleaning", "бельё", "стирка", "laundry",
    "комиссия", "commission", "страховка", "предоплата", "prepay", "кондиционер",
    "газ", "gas", "штраф", "питомцы", "животные", "pets", "кошка", "собака",
    "доставка", "delivery", "трансфер", "налог", "регистрация",
]  # fmt: skip
# Слова, после которых число не цена, хотя сами они ничего не оплачивают.
NOT_A_PRICE_WORDS = [
    "свет", "срок", "term", "duration", "период", "period", "скидка", "discount",
    "торг", "пробег", "mileage", "курс", "телефон", "тел", "phone", "zalo", "whatsapp",
    "hotline",
]  # fmt: skip


@pytest.mark.parametrize("word", [*FEE_WORDS, *NOT_A_PRICE_WORDS])
def test_a_sum_right_after_a_fee_word_is_not_the_price(word: str) -> None:
    assert parse_price(f"{word}: 150 000 VND") is None
    assert parse_price(f"{word} 150 000 VND") is None


@pytest.mark.parametrize("word", FEE_WORDS)
def test_the_same_word_after_without_names_a_feature_not_a_fee(word: str) -> None:
    assert price_of(f"Квартира без {word} 12 млн") == 12_000_000


@pytest.mark.parametrize(
    "unit",
    ["кВт", "квт", "kwh", "kw", "кубометр", "куб", "m3", "м3", "человек", "чел", "person",
     "people", "км", "km", "miles", "кг", "kg", "литр", "л", "cc", "см", "м2", "m2", "кв.м",
     "sqm", "раз", "час", "hour", "hr"],
)  # fmt: skip
def test_a_sum_per_a_physical_unit_is_not_a_price(unit: str) -> None:
    assert parse_price(f"Цена 150 000 VND/{unit}") is None


@pytest.mark.parametrize("amount", ["200 000 ₫ — интернет", "200 000 ₫/мес за питомца"])
def test_a_word_after_the_sum_that_ends_the_clause_names_it(amount: str) -> None:
    assert parse_price(f"✅ {amount}") is None


def test_a_negation_before_the_word_after_the_sum_keeps_it_a_price() -> None:
    assert price_of("Цена: 12 млн — без животных") == 12_000_000
    assert price_of("Цена: 12 млн с парковкой") == 12_000_000


@pytest.mark.parametrize("lead", ["+", "плюс ", "доплата ", "extra ", "дополнительно "])
def test_a_surcharge_is_not_the_price(lead: str) -> None:
    assert price_of(f"Цена: 12 млн\nЗа шлем {lead}500 000 ₫") == 12_000_000


@pytest.mark.parametrize("lead", ["до ", "up to ", "under ", "не более ", "не дороже "])
def test_a_ceiling_of_a_selection_is_not_the_price(lead: str) -> None:
    assert parse_price(f"👉 АРЕНДА {lead}10 МЛН") is None


@pytest.mark.parametrize(
    ("text", "amount"),
    [
        ("Продаю. 6 500 000 ď", 6_500_000),
        ("Продаю. 6 500 000 đ", 6_500_000),
        ("Продаю. 6 500 000 d", 6_500_000),
        ("Цена 6 500 000 vnđ", 6_500_000),
    ],
)
def test_every_spelling_of_the_dong_sign_is_a_currency(text: str, amount: int) -> None:
    assert price_of(text, **BIKE_SALE) == amount


@pytest.mark.parametrize("written", ["у.е.", "у. е.", "дол.", "долл.", "долларов", "usd", "$"])
def test_every_spelling_of_the_dollar_is_a_currency(written: str) -> None:
    facts = parse_prices(f"Цена 300 {written}")

    assert [(fact.amount, fact.currency) for fact in facts] == [(300, "USD")]


@pytest.mark.parametrize("written", ["руб", "рублей", "₽", "rub", "eur", "€"])
def test_a_currency_that_cannot_be_converted_is_not_read_at_all(written: str) -> None:
    """Суммы с запасом над полом правдоподобия: меньшую отсёк бы он, а не валюта."""
    assert parse_prices(f"Цена 150 000 {written}") == []


@pytest.mark.parametrize("text", ["от 15 до 17 млн/мес", "15 to 17 million/month", "15–17 млн/мес"])
def test_a_range_with_a_word_or_a_dash_keeps_both_ends(text: str) -> None:
    fact = parse_price(text, **HOUSE)

    assert fact is not None
    assert (fact.amount, fact.up_to) == (15_000_000, 17_000_000)


@pytest.mark.parametrize(
    ("text", "amount"),
    [
        ("Yamaha NVX 155 — 25 млн ₫", 25_000_000),
        ("Honda Vision 2019 — 12 млн", 12_000_000),
        ("SH 150 - 85 млн", 85_000_000),
    ],
)
def test_a_model_before_a_dash_is_not_the_start_of_a_range(text: str, amount: int) -> None:
    """Вилка не идёт вниз: «NVX 155 — 25 млн» читалось как 155 млн (число с чужой единицей)."""
    assert price_of(text, **BIKE_SALE) == amount


# Слабая единица («к», «m») — деньги, только когда сумму подтверждает что-то ещё.
@pytest.mark.parametrize(
    ("text", "amount"),
    [
        ("Honda 2019, 130к на спидометре, отдам за 15 млн", 15_000_000),
        ("Студия 5 m от моря, отдам за 12 млн", 12_000_000),
        ("Масло моторное 500 ml, отдам за 12 млн", 12_000_000),
    ],
)
def test_a_weak_unit_inside_a_sentence_is_not_money(text: str, amount: int) -> None:
    assert price_of(text) == amount


@pytest.mark.parametrize(
    ("text", "amount"), [("Honda PCX\n36 m", 36_000_000), ("Honda PCX\n130к", 130_000)]
)
def test_a_weak_unit_alone_on_its_line_is_money(text: str, amount: int) -> None:
    assert price_of(text) == amount


def test_a_weak_unit_on_a_line_longer_than_the_window_is_not_alone() -> None:
    assert parse_price("Honda PCX\n36 m" + " " * 300) is None


def test_millilitres_are_not_millions_even_alone_on_a_line() -> None:
    assert parse_price("Масло моторное\n500 ml") is None


@pytest.mark.parametrize(
    "text",
    [
        "Honda Vision\n250000",
        "Honda 125cc 7500000 торг",
        "Honda Vision 2019 года\n2019",
    ],
)
def test_a_bare_number_in_a_plain_text_is_not_a_price(text: str) -> None:
    """Без метки, значка, единицы и срока число — год, пробег или код, а не деньги."""
    assert parse_price(text) is None


def test_a_bare_number_with_a_period_is_a_price_even_without_a_label() -> None:
    assert price_of("Honda Vision 7,500,000/month", deal_type="rent_out") == 7_500_000


def test_a_small_vnd_amount_with_the_letter_d_is_not_scaled_to_millions() -> None:
    """«9 d» — не девять миллионов: буква «d» без единицы слишком часто значит «день»."""
    assert parse_price("Rent for 9 d") is None


def test_a_sum_in_dong_beats_a_sum_in_dollars_of_the_same_rank() -> None:
    """«600$ или 15 млн» — цена в донгах: число в колонке `price_amount` обязано быть донгами."""
    assert price_of("Honda Vision 600$ или 15 млн", **BIKE_SALE) == 15_000_000


def test_a_rate_is_compared_by_its_monthly_equivalent() -> None:
    """Сутки в месяц — тридцать, неделя — четыре: дешевле по месяцу — не по числу в тексте."""
    text = "Цены:\n250 000 VND / сутки\n1 400 000 VND /неделя"

    fact = parse_price(text, **BIKE_RENT)

    assert fact is not None
    assert (fact.amount, fact.period) == (1_400_000, "week")


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Honda Vision\n250 000 VND/месяц", BIKE_RENT),
        ("Honda Vision 600 000 ₫", BIKE_SALE),
        ("Сдам дом 1 500 000 ₫/мес", HOUSE),
    ],
)
def test_a_sum_below_the_floor_of_its_category_is_not_the_price(
    text: str, kind: dict[str, str]
) -> None:
    assert parse_price(text, **kind) is None


# ── второй проход мутаций: слова, у которых был только парный пример ──────────────


def test_the_vietnamese_rent_label_with_a_word_is_still_a_label() -> None:
    """«Giá thuê» — метка целиком: без второго слова она слабая и проигрывает «Giá»."""
    assert price_of(f"{WEAK_RIVAL}Giá thuê 12 млн") == 12_000_000


def test_a_weak_label_never_takes_a_payment_phrase_for_a_price() -> None:
    """«оплата ремонта при покупке 30 млн» — не цена байка, и обгонять цену из текста ей нечем."""
    text = "Honda Vision 2019 — 12 млн\nоплата ремонта при покупке 30 млн"

    assert price_of(text, **BIKE_SALE) == 12_000_000


def test_one_word_before_a_label_still_starts_the_clause() -> None:
    assert price_of(f"{WEAK_RIVAL}Ежемесячная аренда 12 млн") == 12_000_000


def test_a_line_without_a_colon_hands_nothing_to_the_lines_below() -> None:
    """«КВАРТИРА В АРЕНДУ» — не заголовок списка цен, и «8.5» под ней — не 8,5 млн."""
    assert parse_price("КВАРТИРА В АРЕНДУ\n8.5", **APARTMENT) is None


def test_a_header_with_digits_hands_nothing_to_the_lines_below() -> None:
    assert parse_price("🟡 Условия аренды №3:\n8.5", **APARTMENT) is None


def test_a_header_with_a_colon_does_hand_its_label_to_the_lines_below() -> None:
    assert price_of("💰 Цены:\n8.5", **APARTMENT) == 8_500_000


@pytest.mark.parametrize(
    "tail", ["торг", "торгуемся", "нег", "nego", "включая интернет", "вкл", "net", "all in"]
)
def test_a_word_of_the_price_after_a_listed_number_keeps_it_a_price(tail: str) -> None:
    assert price_of(f"💰 Цены:\n• 8.5 {tail}", **APARTMENT) == 8_500_000


@pytest.mark.parametrize(
    "tail", ["покупка", "продажа", "аренда", "rent", "buy", "sale", "sell", "выкуп"]
)
def test_a_deal_word_after_a_listed_number_keeps_it_a_price(tail: str) -> None:
    assert price_of(f"💰 Цены:\n• 8.5 {tail}", **APARTMENT) == 8_500_000


def test_a_count_with_a_dash_before_the_noun_is_not_a_price_even_next_to_the_word_price() -> None:
    assert parse_price("Цена 2-month contract", **APARTMENT) is None
    assert parse_price("Цена 3 - 6 month contract", **APARTMENT) is None
    assert parse_price("Цена 2, 3 этаж", **APARTMENT) is None


def test_a_list_of_items_under_an_included_header_is_not_a_list_of_prices() -> None:
    """Без заголовка «включено» строка «2 (шлема)» под меткой читалась бы как 2 млн."""
    text = "⭐ 5 000 000 VND / месяц\nВ стоимость включено:\n• 2 (шлема)"

    assert price_of(text, **BIKE_RENT) == 5_000_000
    assert parse_price("Аренда включает:\n• 2 (шлема)", **BIKE_RENT) is None
    assert parse_price("Аренда:\n• 2 (шлема)", **BIKE_RENT) is not None


def test_a_fee_word_far_from_the_sum_names_nothing() -> None:
    """Слово «за что платят» называет сумму вплотную, а не из середины предложения."""
    assert (
        price_of("Интернет и кондиционер новые свежий ремонт 12 млн/мес", **APARTMENT) == 12_000_000
    )


@pytest.mark.parametrize(
    "text",
    [
        "Apartment with parking 15 млн",
        "Flat without commission 15 млн",
        "no deposit 15 млн",
        "free wifi 15 млн",
        "Квартира бесплатный интернет 15 млн",
        "Квартира including internet 15 млн",
        "Квартира incl wifi 15 млн",
        "Квартира со страховкой 15 млн",
        "Квартира с большой парковкой 15 млн",
        "Квартира без залога 15 млн",
    ],
)
def test_a_feature_word_before_a_fee_word_leaves_the_sum_a_price(text: str) -> None:
    assert price_of(text, **APARTMENT) == 15_000_000


@pytest.mark.parametrize("lead", ["+", "плюс ", "доплата ", "extra ", "дополнительно "])
def test_a_surcharge_next_to_the_price_never_wins_as_the_cheapest(lead: str) -> None:
    """Без категории границ нет: 500 тысяч отсекает только слово перед ними."""
    assert price_of(f"Honda Vision 12 млн {lead}500 000 ₫") == 12_000_000


@pytest.mark.parametrize(
    ("text", "period"),
    [
        ("Аренда 120 млн в год", "year"),
        ("Аренда 120 млн/год", "year"),
        ("Rent 120 million per year", "year"),
        ("Giá 120 triệu/năm", "year"),
        ("Аренда 9 млн в месяц", "month"),
        ("Аренда 9 млн/мес.", "month"),
        ("Rent 9 million per month", "month"),
        ("Giá 9 triệu/tháng", "month"),
        ("Аренда 1,4 млн в неделю", "week"),
        ("Rent 1,4 million per week", "week"),
        ("Giá 1,4 triệu/tuần", "week"),
        ("Аренда 250 тыс в сутки", "day"),
        ("Аренда 250 тыс за ночь", "day"),
        ("Rent 250k per day", "day"),
        ("Giá 250k/ngày", "day"),
        ("Месяц: 9 млн", "month"),
        ("Неделя: 1,4 млн", "week"),
        ("Сутки: 250 тыс", "day"),
        ("Посуточно 250 тыс", "day"),
        ("посуточная аренда: 250 тыс", "day"),
        ("понедельная аренда: 1,4 млн", "week"),
        ("Daily rent: 250k", "day"),
        ("Weekly rent: 1,4 million", "week"),
    ],
)
def test_the_period_of_a_price_is_read_from_every_place_it_is_written(
    text: str, period: str
) -> None:
    (fact,) = parse_prices(text)

    assert fact.period == period


@pytest.mark.parametrize(
    "text",
    [
        "Цена 9 млн годится",
        "Цена 9 млн деньги вперёд",
        "Цена 9 млн место осмотра",
        "Цена 9 млн дневной",
    ],
)
def test_a_word_that_only_starts_like_a_period_gives_no_period(text: str) -> None:
    (fact,) = parse_prices(text)

    assert fact.period is None


def test_a_rate_is_compared_by_its_monthly_equivalent_without_bounds_to_help() -> None:
    """Границы категории выкинули бы лишнюю ставку сами, и коэффициенты остались бы нетронутыми."""
    day_cheaper = "Цены:\n200 000 VND / сутки\n1 800 000 VND /неделя"
    week_cheaper = "Цены:\n250 000 VND / сутки\n1 400 000 VND /неделя"

    day, week = (
        parse_price(day_cheaper, deal_type="rent_out"),
        parse_price(week_cheaper, deal_type="rent_out"),
    )

    assert day is not None
    assert week is not None
    assert (day.period, week.period) == ("day", "week")


def test_glued_tail_of_a_word_is_not_the_start_of_a_range() -> None:
    """«35м2-12 млн»: «2» — хвост «м2», а тире без пробелов вилку не делает сама по себе."""
    assert price_of("Студия 35м2-12 млн/мес", **APARTMENT) == 12_000_000


def test_a_number_orders_of_magnitude_below_the_sum_is_not_the_start_of_a_range() -> None:
    assert price_of("Цена: 10–11 000 000 VND/месяц", **APARTMENT) == 11_000_000


def test_the_aggregator_line_is_recognised_only_on_a_short_line() -> None:
    """Пробелы без конца не делают строку итогом агрегатора: длинную строку читаем как есть."""
    assert parse_prices("💰 9 000 000 ₫ #booking") == []
    assert [fact.amount for fact in parse_prices("💰 9 000 000 ₫ #booking" + " " * 300)] == [
        9_000_000
    ]


def test_a_listed_number_followed_by_a_period_phrase_is_a_price() -> None:
    """«8.5 в месяц» — слово «в» за числом не делает его счётом: срок за ним — признак цены."""
    assert price_of("💰 Цены:\n• 8.5 в месяц", **APARTMENT) == 8_500_000


def test_a_fee_word_before_a_label_does_not_name_the_sum_after_the_label() -> None:
    """Слова до метки смотрим только после неё: «депозит отдельно цена 12 млн» — это цена."""
    assert price_of("Депозит отдельно цена 12 млн", **APARTMENT) == 12_000_000


def test_a_price_in_the_other_deal_label_is_not_the_price_of_this_deal() -> None:
    """«Стоимость покупки» в объявлении об аренде — цена покупки, а не аренды."""
    rent = "Студия: 15 млн (вид на город)\n🔰Стоимость покупки 2 млн"

    assert price_of(rent, **APARTMENT) == 15_000_000
    assert price_of("Продам байк: 15 млн\nАренда 2 млн", **BIKE_SALE) == 15_000_000
    assert price_of("Продам байк 15 млн\nGiá thuê 2 млн", **BIKE_SALE) == 15_000_000


def test_a_reservation_fee_is_not_the_price() -> None:
    assert price_of("Студия 7 млн/мес. Резерв 2 млн после осмотра", **APARTMENT) == 7_000_000


def test_ml_with_a_period_and_no_label_is_millions() -> None:
    assert price_of("Studio 15ml/month", **APARTMENT) == 15_000_000
