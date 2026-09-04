"""Модель — отдельное понятие, и выдача обязана это чувствовать.

Живая жалоба владельца 02.09.2026: на «нужен скутер, не мотоцикл, honda lead»
бот отдал Honda Airblade трижды и лот 59-дневной давности. Дефекта было два, и
они независимы.

Первый: понятия МОДЕЛИ в системе не существовало. Марки и модели лежали одним
списком, побеждало первое совпадение regex — «honda lead» читалось как «honda»,
и дальше по всей цепочке (план, фильтры доски, ранжирование) слово «lead» не
участвовало нигде.

Второй: живой поиск только СОРТИРОВАЛ. Ниже — не значит «не показан»: карточек
показывается пять, и когда лучшего нет, первыми пятью оказывается мусор.

Поэтому здесь проверяется не «функция вернула значение», а поведение обеих
починок целиком: от слов клиента до того, что уедет источнику и что дойдёт до
карточек.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sniffer.bot.cards import render_cards
from sniffer.domain.dialogue import Feedback, feedback_buttons, next_question
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.search.fallback import fallback_plan
from sniffer.search.intake import merge
from sniffer.search.intake_rules import detect_brand, detect_model, parse_query, with_model_facts
from sniffer.search.motorbike_models import BODY_ELECTRIC, BODY_MANUAL, MOTORBIKE_MODELS
from sniffer.search.relevance import LIVE_MAX_AGE_DAYS, rank_items
from sniffer.search.vocabulary import (
    model_category,
    model_engine_cc,
    model_transmission,
    models_named_in,
)
from sniffer.sources.base import RawItem
from sniffer.sources.chotot import build_params

CITY = "nha_trang"
NOW = datetime(2026, 9, 2, tzinfo=UTC)
RATE = 26_000.0


def wants(**attributes: Any) -> Passport:
    """Паспорт клиента, который уже назвал предмет и город."""
    return Passport(
        intent=Intent.BUY,
        category=Category.MOTORBIKE,
        city=CITY,
        budget=Budget(max=500, currency=Currency.USD),
        attributes=attributes,
        raw_query="нужен скутер honda lead",
    )


def lot(title: str, *, age_days: float = 0.5, price: int | None = 12_000_000) -> RawItem:
    posted = None if age_days < 0 else NOW - timedelta(days=age_days)
    return RawItem(
        source="telegram_groups",
        external_id=title,
        url=f"https://example.test/{title}",
        title=title,
        price_vnd=price,
        posted_at=posted,
    )


def shown(passport: Passport, items: list[RawItem]) -> list[str]:
    return [item.external_id for item in rank_items(passport, items, usd_vnd=RATE, now=NOW)]


# ── 1. Марка и модель — два разных факта ────────────────────────────────────


def test_the_brand_no_longer_swallows_the_model() -> None:
    """Жалоба дословно: «honda lead» уходило в поиск как «honda».

    Первое совпадение regex по списку, где вперемешку производители и модели,
    — это и есть дефект: побеждало то, что раньше лежит в таблице.
    """
    passport = parse_query("нужен скутер, не мотоцикл, honda lead", default_city=CITY)

    assert passport.attributes["brand"] == "honda"
    assert passport.attributes["model"] == "lead"


def test_word_order_does_not_decide_which_is_which() -> None:
    """Порядок слов в запросе — не признак марки: обе формулировки об одном."""
    straight = parse_query("honda lead до 500", default_city=CITY).attributes
    reversed_words = parse_query("lead honda до 500", default_city=CITY).attributes

    assert straight["brand"] == reversed_words["brand"] == "honda"
    assert straight["model"] == reversed_words["model"] == "lead"


def test_a_model_alone_names_its_brand() -> None:
    """«Лид» без слова «хонда» — всё равно Honda: марка следует из таблицы."""
    passport = parse_query("нужен лид до 500", default_city=CITY)

    assert passport.attributes["model"] == "lead"
    assert passport.attributes["brand"] == "honda"


def test_a_spelling_with_and_without_a_space_is_one_model() -> None:
    """«Air Blade» и «airblade» — одно имя, а не два разных байка."""
    assert detect_model("продам air blade 2021") == "air_blade"
    assert detect_model("продам airblade 2021") == "air_blade"


def test_the_longest_spelling_wins_not_the_table_order() -> None:
    """«winner x» конкретнее «winner», и решает это написание, а не порядок строк."""
    bikes = Category.MOTORBIKE

    assert models_named_in(bikes, "Honda Winner X 2021")[0] == "winner"
    # Названо две модели — ответ один и тот же при любом порядке слов.
    assert models_named_in(bikes, "lead или vision") == models_named_in(bikes, "vision или lead")


def test_a_building_named_vision_is_not_a_honda() -> None:
    """У жилья моделей нет, и это таблица, а не проверка «а мотобайк ли это».

    Без неё «квартира Vision» уводила бы и план поиска (первым запросом чата
    стало бы «honda vision»), и отбор выдачи — он по модели отсекает.

    С выводом категории из модели у этого теста появилась вторая работа: он
    сторожит направление вывода. Категория названа словом, значит имя модели в
    этом тексте — название дома, и обратный ход по таблице обязан молчать.
    """
    flat = parse_query("сниму квартиру Vision в Нячанге", default_city=CITY)

    assert flat.category is Category.APARTMENT
    assert "model" not in flat.attributes
    assert "brand" not in flat.attributes
    assert "transmission" not in flat.attributes


def test_a_model_without_a_category_is_still_read() -> None:
    """«honda lead» без слова «скутер» — обычная формулировка, и модель в ней настоящая.

    Категория при этом не остаётся пустой: имя модели называет предмет, и с
    02.09.2026 она из него следует (см. соседний тест). Раньше здесь стояло
    `category is None` — это было описанием пробела, а не требованием.
    """
    passport = parse_query("honda lead 2019", default_city=CITY)

    assert passport.attributes["model"] == "lead"
    assert passport.category is Category.MOTORBIKE


def test_an_unknown_model_leaves_the_search_as_it_was() -> None:
    """Таблица короткая намеренно: незнакомое имя — прежнее поведение, а не брак.

    Ложная модель отрезала бы верную выдачу, поэтому «SH» остаётся неузнанным, и
    искать бот будет по марке — ровно как раньше.
    """
    passport = parse_query("куплю honda sh до 500", default_city=CITY)

    assert "model" not in passport.attributes
    assert passport.attributes["brand"] == "honda"


def test_the_brand_question_still_understands_a_model_in_words() -> None:
    """На «есть марка на примете?» отвечают моделью — это ответ, а не новый запрос."""
    assert detect_brand("хочу yamaha") == "yamaha"
    assert detect_brand("да лид какой-нибудь") == "honda"


# ── 2. Что следует из модели ────────────────────────────────────────────────


def test_a_scooter_model_fills_the_gearbox_by_itself() -> None:
    """Honda Lead — скутер, а у скутера вариатор. Спрашивать тут нечего."""
    passport = parse_query("нужен скутер honda lead", default_city=CITY)

    assert passport.attributes["transmission"] == "automatic"


@pytest.mark.parametrize(
    ("model", "transmission"),
    [("lead", "automatic"), ("wave", "semi"), ("exciter", "manual"), ("klara", None)],
)
def test_the_gearbox_follows_the_body_not_a_second_column(
    model: str, transmission: str | None
) -> None:
    """Xe số — полуавтомат, côn tay — механика, электро — вывести нельзя.

    У `klara` ответ `None` не от нехватки данных: у `xe điện` своё значение поля
    (`motorbiketype=4`), и «автомат» отправил бы на доску фильтр, который
    электробайки как раз исключает.
    """
    assert model_transmission(model) is transmission


def test_the_category_follows_from_the_model() -> None:
    """Жалоба владельца дословно: «ищу хонду вижн до 400» — и бот спрашивал «Что ищем?».

    Vision не бывает ничем, кроме мотобайка, — предмет назван, и вопрос о нём и
    есть та «тупизна». Категория берётся обратным ходом по `MODELS_BY_CATEGORY`,
    а не второй таблицей: две таблицы об одном однажды разъедутся.
    """
    passport = parse_query("ищу хонду вижн до 400", default_city=CITY)

    assert passport.category is Category.MOTORBIKE
    assert passport.attributes["model"] == "vision"
    assert "category" not in passport.missing_fields


@pytest.mark.parametrize("model", [model.slug for model in MOTORBIKE_MODELS])
def test_every_model_of_the_table_names_its_own_category(model: str) -> None:
    """Обратный ход отвечает на КАЖДОЕ имя ряда, а не на те, что вспомнили.

    Список в тесте связан с таблицей механически: допишут модель — она попадёт
    сюда сама, и «а эту забыли» не станет молчаливым пробелом.
    """
    assert model_category(model) is Category.MOTORBIKE


def test_an_unknown_name_derives_no_category() -> None:
    """Неизвестная МОДЕЛЬ категории не выдумывает: «sh» сама по себе — ничто.

    Категорию теперь выводит и марка (все марки рынка мотобайковые), поэтому
    «honda sh» — мотобайк ПО МАРКЕ «honda», а не по неизвестной модели «sh».
    Проверяем обе стороны: имя неизвестной модели без марки категории не даёт,
    а известная марка — даёт (defect 02.09.2026: «yamaha» оставалась без категории).
    """
    assert model_category("sh") is None
    assert parse_query("куплю sh до 500", default_city=CITY).category is None
    assert parse_query("куплю honda sh до 500", default_city=CITY).category is Category.MOTORBIKE


def test_the_client_outranks_the_table() -> None:
    """«Lead на механике» не бывает, но спорить с клиентом здесь не наше дело.

    Та же дисциплина, что у объёма двигателя: выведенное значение ложится
    только на пустое место.
    """
    kept = with_model_facts({"model": "lead", "transmission": "manual", "brand": "yamaha"})

    assert kept["transmission"] == "manual"
    assert kept["brand"] == "yamaha"


def test_a_gearbox_named_in_words_outranks_the_one_derived_from_the_model() -> None:
    """«lead механика»: у Lead в таблице автомат, но клиент сказал иначе — вслух.

    Проверяется весь путь от слов, а не одна `with_model_facts`: коробку теперь
    читает и первичный запрос, и порядок этих двух знаний решает, чей ответ
    окажется в паспорте.
    """
    passport = parse_query("lead механика", default_city=CITY)

    assert passport.attributes["model"] == "lead"
    assert passport.attributes["transmission"] == "manual"


def test_an_electric_model_gets_no_invented_gearbox() -> None:
    """Марка выводится, коробка — нет: приписать её значило бы отрезать выдачу."""
    derived = with_model_facts({"model": "klara"})

    assert derived["brand"] == "vinfast"
    assert "transmission" not in derived


# ── 3. Диалог не спрашивает о том, что уже знает ────────────────────────────


def test_the_dialogue_does_not_ask_about_a_gearbox_it_derived() -> None:
    """Жалоба владельца: на «нужен скутер honda lead» бот спрашивал про коробку.

    Контрольная половина обязательна: без модели вопрос по-прежнему задаётся —
    иначе тест доказывал бы, что вопрос исчез вообще, а не что он лишний.
    """
    with_model = parse_query("нужен скутер honda lead", default_city=CITY)
    without_model = parse_query("нужен скутер", default_city=CITY)
    # Бюджет информативнее коробки, поэтому его спрашивают первым: чтобы вопрос
    # о коробке вообще дошёл до очереди, бюджет должен быть уже спрошен.
    asked = ("budget.max",)

    derived = next_question(with_model, asked)
    unknown = next_question(without_model, asked)

    assert derived is not None and derived.field != "attributes.transmission"
    assert unknown is not None and unknown.field != "attributes.transmission"


def test_the_automatic_button_disappears_when_the_answer_is_known() -> None:
    """«Нужен автомат» под карточками Lead — кнопка про уже заполненное поле."""
    lead = wants(model="lead", transmission="automatic")
    kinds = {option.value for option in feedback_buttons(lead)}
    without_model = {option.value for option in feedback_buttons(wants())}

    assert Feedback.AUTOMATIC.value not in kinds
    assert Feedback.AUTOMATIC.value in without_model


# ── 4. Модель доезжает до чата, но не до доски ──────────────────────────────


def test_the_model_becomes_the_first_query_of_a_chat() -> None:
    """В чате объявление пишут прозой: «Продам Honda Lead 2019» — это текст."""
    plan = fallback_plan(wants(model="lead", brand="honda"), ["telegram_groups"], reason="тест")

    assert plan.tasks[0].query.startswith("honda lead")


def test_the_model_never_reaches_the_board_as_text() -> None:
    """У доски поля модели нет, а `q` гасит те фильтры, что есть.

    Замер (spec-v2, 4.1.1): `motorbiketype=3` даёт 12 объявлений, он же с любым
    словом в `q` — ноль. Имя модели здесь не исключение: доска складывает `q` с
    фильтрами через И.
    """
    plan = fallback_plan(wants(model="lead", brand="honda"), ["chotot"], reason="тест")
    built = [build_params(task.query, task.params) for task in plan.tasks]

    assert all("q" not in params for params in built)
    # Марка при этом уезжает полем — она у доски есть.
    assert all(params["motorbikebrand"] == 1 for params in built)


# ── 5. Чужая модель до карточек не доходит ──────────────────────────────────


def test_another_model_does_not_reach_the_cards() -> None:
    """Три Airblade на запрос Lead — это брак выдачи, а не низкий балл."""
    airblades = [lot(f"Honda Air Blade 2021 #{index}") for index in range(3)]
    lead = lot("Honda Lead 2019", age_days=3)

    assert shown(wants(model="lead"), [*airblades, lead]) == ["Honda Lead 2019"]


def test_a_specific_model_requires_positive_evidence() -> None:
    """На запрос Lead безымянный скутер не точнее честного пустого ответа."""
    silent = lot("Скутер Хонда 2019, автомат, блюкарт")

    assert shown(wants(model="lead"), [silent, lot("Honda Air Blade 2021")]) == []


def test_no_lead_on_the_market_is_an_honest_empty_answer() -> None:
    """Единственная ступень, которая не отменяется, — и это осознанно.

    Пустой ответ бота прямо советует «попробуйте без марки», а пять Airblade на
    запрос Lead — ровно то, на что жаловался владелец: клиент читает их как «бот
    меня не услышал».
    """
    assert shown(wants(model="lead"), [lot("Honda Air Blade 2021"), lot("Yamaha Sirius")]) == []


def test_the_named_model_is_the_only_proven_match() -> None:
    """Неизвестный скутер не подмешивается к явно найденному Lead."""
    named = lot("Honda Lead 2019", age_days=1)
    silent = lot("Скутер, автомат", age_days=1)

    assert shown(wants(model="lead"), [silent, named]) == [named.title]


# ── 6. Порог показа и подстраховка ──────────────────────────────────────────


def test_a_two_month_old_lot_loses_to_a_fresh_one() -> None:
    """Тот самый 59-дневный лот. Сортировка его не убирала — показывали пять."""
    stale = lot("Honda Lead 2018", age_days=59)
    fresh = lot("Honda Lead 2021", age_days=2)

    assert shown(wants(model="lead"), [stale, fresh]) == [fresh.title]


def test_the_only_lot_is_shown_even_when_it_is_old() -> None:
    """Пустая выдача хуже слабой, и честность берёт на себя карточка."""
    stale = lot("Honda Lead 2018", age_days=59)

    assert shown(wants(model="lead"), [stale]) == [stale.title]
    assert "могло быть продано" in render_cards([stale], now=NOW)


def test_the_threshold_is_a_date_not_a_score() -> None:
    """Лот без даты — не старый, а неизвестный, и карточка это скажет.

    Балльный порог (как `MATCH_MIN_SCORE` у подписки) здесь отсекал бы обоих
    одинаково: `freshness` отдаёт ноль и лоту без даты, и двухмесячному.
    """
    undated = lot("Honda Lead без даты", age_days=-1)

    assert shown(wants(model="lead"), [undated]) == [undated.title]
    assert "дата публикации неизвестна" in render_cards([undated], now=NOW)


@pytest.mark.parametrize("age_days", [LIVE_MAX_AGE_DAYS - 1, LIVE_MAX_AGE_DAYS])
def test_a_lot_within_the_threshold_stays(age_days: int) -> None:
    """Порог отсекает то, что СТАРШЕ его, а не то, что ему равно."""
    old = lot("Honda Lead 2019", age_days=age_days)
    fresh = lot("Honda Lead 2022", age_days=1)

    assert old.title in shown(wants(model="lead"), [old, fresh])


# ── 7. Тот же вывод на ответе модели ────────────────────────────────────────


def test_the_model_answer_is_normalised_to_the_table() -> None:
    """LLM пишет то «Honda Lead», то «air blade» — в паспорте лежит слаг.

    Иначе фильтру выдачи не с чем сверять, а вывод марки и коробки работал бы
    только на пути без брокера, то есть незаметно ломался бы в бою.
    """
    rules = parse_query("нужен скутер", default_city=CITY)

    merged = merge(rules, {"category": "motorbike", "brand": "", "model": "Honda Lead"})

    assert merged.attributes["model"] == "lead"
    assert merged.attributes["brand"] == "honda"
    assert merged.attributes["transmission"] == "automatic"


def test_an_unknown_model_from_the_answer_is_dropped() -> None:
    """Чужая модель хуже пустой: пустая — прежнее поведение, чужая — брак."""
    rules = parse_query("нужен скутер", default_city=CITY)

    merged = merge(rules, {"category": "motorbike", "brand": "Honda", "model": "SH Mode"})

    assert "model" not in merged.attributes
    assert merged.attributes["brand"] == "honda"


def test_pcx_is_read_in_cyrillic() -> None:
    """«пцх» — то, как PCX пишут кириллицей, и без написания модель терялась.

    У Lead есть «лид», у Vision «вижн», а PCX оставался одной латиницей, и на
    «хочу пцх» бот не узнавал ни модель, ни категорию — спрашивал «что ищем?»
    там, где предмет назван. Аудит написаний по базе Нячанга (realcheck).
    """
    passport = parse_query("хочу пцх", default_city=CITY)

    assert passport.category is Category.MOTORBIKE
    assert passport.attributes["model"] == "pcx"
    assert passport.attributes["brand"] == "honda"


def test_a_listing_named_only_by_its_model_still_has_a_category() -> None:
    """«Honda Lead 110 2008» не говорит «скутер», но Lead бывает лишь у мотобайка.

    Сторона лота обязана выводить категорию из модели так же, как сторона
    запроса. Без этого лот, назвавший только модель, для запроса о жилье —
    «неизвестная категория», и проходит фильтр (realcheck 03.09.2026: Honda Lead
    в выдаче студий). Имя дома исключение не ломает: категорию там называет слово
    («квартира»), и до модели дело не доходит.
    """
    from sniffer.search.intake_rules import category_of

    assert category_of("Продам Honda Lead 110 2008 вариатор инжектор") is Category.MOTORBIKE
    assert category_of("Сдам студию у моря, есть мебель") is Category.APARTMENT
    assert category_of("Квартира Vision Tower, 2 спальни") is Category.APARTMENT


# ── 8. Замер частоты по базе: новые модели ──────────────────────────────────


@pytest.mark.parametrize(
    ("query", "model", "brand", "transmission"),
    [
        ("продам yamaha r15 2019", "r15", "yamaha", "manual"),
        ("нужен sym attila", "attila", "sym", "automatic"),
        ("kymco hermosa до 500", "hermosa", "kymco", "automatic"),
        ("honda click 2021", "click", "honda", "automatic"),
    ],
)
def test_a_measured_model_names_its_brand_and_gearbox(
    query: str, model: str, brand: str, transmission: str
) -> None:
    """Имя измерено по базе Нячанга (realcheck 03.09.2026), кузов — справочный.

    R15 — côn tay, значит МЕХАНИКА: спортбайк, а не скутер. Attila, Hermosa, Click
    — вариаторы, значит автомат. Марка и коробка следуют из модели той же
    таблицей, как у Lead и Exciter, — отдельного разбора им не нужно.
    """
    passport = parse_query(query, default_city=CITY)

    assert passport.attributes["model"] == model
    assert passport.attributes["brand"] == brand
    assert passport.attributes["transmission"] == transmission
    assert passport.category is Category.MOTORBIKE


def test_attila_is_read_in_cyrillic() -> None:
    """«аттила» русскоязычный Нячанг пишет кириллицей, и оно однозначно.

    Гунн Аттила в запросе о байке или жилье не встретится — в отличие от «клары»
    (женское имя), у которой кириллицу как раз убрали. Поэтому у Attila она есть,
    а у Hermosa/R15/Click — нет: за них кириллицей не поручиться.
    """
    passport = parse_query("хочу аттила", default_city=CITY)

    assert passport.attributes["model"] == "attila"
    assert passport.attributes["brand"] == "sym"


def test_an_r15_lot_named_only_by_its_model_has_a_category() -> None:
    """«Продам Yamaha R15 2019» слова «мотоцикл» не говорит, но R15 бывает лишь у байка.

    До внесения имени такой лот для запроса о жилье был «неизвестной категорией»
    и проходил фильтр чужой категории — тот же класс дефекта, что Honda Lead в
    выдаче студий (realcheck 03.09.2026). Имя дома исключение не ломает: категорию
    там называет слово, и до модели дело не доходит.
    """
    from sniffer.search.intake_rules import category_of

    assert category_of("Продам Yamaha R15 2019, механика, идеал") is Category.MOTORBIKE
    assert category_of("Сдам студию у моря, длительный срок") is Category.APARTMENT


def test_a_bare_r15_lot_is_dropped_on_a_housing_query() -> None:
    """Тот же вывод, но на всю цепочку выдачи: на запрос квартиры R15-лот не всплывает.

    Утечка была именно выдачей: лот, назвавший одну модель без слова категории,
    для запроса о жилье «неизвестная категория» и проходил в карточки по свежести.
    Теперь категория следует из модели и на стороне лота — байк уходит отсевом.
    """
    wants_flat = Passport(
        intent=Intent.RENT,
        category=Category.APARTMENT,
        city=CITY,
        raw_query="сниму студию у моря",
    )
    bare_bike = lot("Yamaha R15 2019, механика, идеал")
    flat = lot("Студия с мебелью, длительный срок")

    assert shown(wants_flat, [bare_bike, flat]) == [flat.title]


def test_candy_is_deliberately_not_a_model() -> None:
    """Kymco Candy измерен (103), но «candy» — английское слово, бренд стиралок и
    название краски на самих байках («màu candy»). Ложная модель режет верную
    выдачу — тот же довод, что у «SH», — поэтому его нет. Тест сторожит РЕШЕНИЕ.

    Вторая проверка показывает ровно тот промах, которого избегаем: «candy» как
    цвет на честном лоте Lead не поднимает чужую модель и не рушит его выдачу.
    """
    assert "candy" not in {model.slug for model in MOTORBIKE_MODELS}
    assert detect_model("Honda Lead màu candy đỏ", Category.MOTORBIKE) == "lead"
    assert model_category("candy") is None


def test_excel_stays_out_until_its_body_is_known() -> None:
    """SYM Excel измерен (41), марка ясна (SYM), а кузов — нет.

    Коробка следует из кузова, поэтому внести модель значит записать коробку;
    записать её, не проверив, — выдать догадку за замер, что файл и запрещает.
    Строка появится, когда кузов будет проверен, а не угадан.
    """
    assert "excel" not in {model.slug for model in MOTORBIKE_MODELS}


# ── 9. Представительный объём модели ────────────────────────────────────────


@pytest.mark.parametrize(
    ("model", "engine_cc"),
    [("lead", 110), ("pcx", 150), ("nvx", 155), ("r15", 155), ("hermosa", 50), ("vespa", 125)],
)
def test_a_model_carries_its_representative_displacement(model: str, engine_cc: int) -> None:
    """Объём справочный и огрублённый: одно частое число на ряд вариантов.

    Годится расставить лот по КЛАССУ, когда сам лот объём не назвал (R15 — 155, а
    значит не 250), а спорить о ±10 см³ не берётся: вариантов у модели больше
    одного, и это записано в провенансе таблицы честно.
    """
    assert model_engine_cc(model) == engine_cc


def test_an_electric_model_has_no_displacement() -> None:
    """Klara — электро: объёма нет вовсе (`None`), как и коробки.

    Приписать электробайку число значило бы отсечь его на «от 250» выдуманным
    объёмом — у `xe điện` объёма физически не существует.
    """
    assert model_engine_cc("klara") is None


def test_an_unknown_name_has_no_displacement() -> None:
    """Незнакомое имя объёма не выдумывает — прежнее поведение, а не догадка."""
    assert model_engine_cc("sh") is None
    assert model_engine_cc("") is None
    assert model_engine_cc(None) is None


@pytest.mark.parametrize("model", [model.slug for model in MOTORBIKE_MODELS])
def test_every_model_declares_a_displacement_or_an_honest_none(model: str) -> None:
    """Каждая модель ряда явно объявляет объём или честный `None` — молчком не пропущена.

    Список связан с таблицей механически: допишут модель — она попадёт сюда сама,
    и «а этой объём забыли» не станет тихим пробелом. Но «честный `None`» теперь
    зависит от кузова, и это не послабление, а разное знание:

    * **скутер и лапка** (`tay ga`, `xe số`) объём в имени НЕ несут («Lead»,
      «Wave»), поэтому представительный объём — стабильный факт модели, и его
      отсутствие было бы пропуском. Число обязательно и в границах мотобайка
      (49..2000 см³, как `engine_size.MIN_CC`..`MAX_CC`);
    * **механика** (`côn tay`) — спортивные семейства, у которых объём стоит в
      САМОМ имени и у семейства РАЗНЫЙ (CBR150 против CBR650, Z300 против Z900):
      одно число соврало бы на всех членах, кроме одного, поэтому `None` здесь
      честнее числа, а настоящий объём читается из текста лота. Конкретная модель
      со стабильным объёмом (R15 — 155, Exciter — 150) число объявить ВПРАВЕ, и
      тогда оно в границах — но не обязана;
    * **электро** — объёма нет физически, только `None`.

    Так тест ловит скутер, забывший объём, и электро с выдуманным числом, но не
    мешает механике честно молчать (`None` теперь и НЕ у электро).
    """
    body = {entry.slug: entry.body for entry in MOTORBIKE_MODELS}[model]
    cc = model_engine_cc(model)

    if body == BODY_ELECTRIC:
        assert cc is None
    elif body == BODY_MANUAL:
        assert cc is None or 49 <= cc <= 2000
    else:
        assert cc is not None and 49 <= cc <= 2000


# ── 10. Мотоциклетные семейства: спорт, нейкед, круизёр, эндуро ──────────────


@pytest.mark.parametrize(
    ("query", "model", "brand"),
    [
        # Honda
        ("нужен honda cbr", "cbr", "honda"),
        ("продам honda cb1000r", "cb", "honda"),
        ("honda rebel 300", "rebel", "honda"),
        ("honda crf 250", "crf", "honda"),
        ("honda xr150", "xr", "honda"),
        # Yamaha (r15/exciter уже в таблице — здесь новые)
        ("yamaha mt", "mt", "yamaha"),
        ("yamaha r3 2020", "r3", "yamaha"),
        ("yamaha r25", "r25", "yamaha"),
        ("yamaha fz150", "fz", "yamaha"),
        # Kawasaki
        ("ninja", "ninja", "kawasaki"),
        ("kawasaki z300", "z", "kawasaki"),
        ("kawasaki zx-10r", "zx", "kawasaki"),
        # Suzuki
        ("suzuki gsx", "gsx", "suzuki"),
        ("suzuki raider 150", "raider", "suzuki"),
        # KTM (новая марка)
        ("ktm duke", "duke", "ktm"),
        ("ktm rc390", "rc", "ktm"),
        # Ducati
        ("ducati monster", "monster", "ducati"),
        ("ducati panigale", "panigale", "ducati"),
    ],
)
def test_a_sport_family_names_its_brand_gearbox_and_category(
    query: str, model: str, brand: str
) -> None:
    """Мотоциклетное семейство узнаётся моделью, марка и коробка следуют из него.

    Кузов у всех côn tay → МЕХАНИКА (спорт/нейкед/круизёр/эндуро), а не скутер:
    ровно этого и не хватало — «honda cbr» возвращал Honda PSX (чужой скутер),
    «kawasaki z300» не узнавалось вовсе. Марка выводится из модели той же таблицей
    (ninja → kawasaki, duke → ktm), категория — motorbike обратным ходом.
    """
    passport = parse_query(query, default_city=CITY)

    assert passport.attributes["model"] == model
    assert passport.attributes["brand"] == brand
    assert passport.attributes["transmission"] == "manual"
    assert passport.category is Category.MOTORBIKE


def test_r15_and_exciter_are_not_duplicated_by_the_new_families() -> None:
    """r15/exciter стояли в таблице до этой правки — вносить их заново нельзя."""
    slugs = [model.slug for model in MOTORBIKE_MODELS]

    assert slugs.count("r15") == 1
    assert slugs.count("exciter") == 1
    # А новые семейства при этом на месте и не столкнулись слагами.
    assert {"cbr", "mt", "z", "duke", "monster"} <= set(slugs)


@pytest.mark.parametrize("query", ["yamaha mt-15", "yamaha mt 15", "продам mt15 2022"])
def test_mt_is_read_stuck_spaced_or_hyphenated(query: str) -> None:
    """Продавец пишет объём слитно, через дефис и через пробел — всё это MT.

    Клиент же спрашивает голым «yamaha mt» без числа — это ловит якорь по марке,
    а формы продавца — якорь по цифре. Оба нужны: убери любой, и половина живых
    написаний из снимка Нячанга (mt-15, mt-03, mt 15, mt15) перестанет узнаваться.
    """
    assert detect_model(query, Category.MOTORBIKE) == "mt"


def test_cb_and_cbr_are_two_different_families() -> None:
    """«cb» и «cbr» различает буква после префикса, а не порядок строк.

    Якорь «cb» — по цифре (`cb\\d`), поэтому «cbr150r» (после cb стоит r) им не
    ловится, а ловится своим «cbr». Иначе один лот читался бы двумя моделями, и
    отсев по модели стал бы случайным.
    """
    bikes = Category.MOTORBIKE

    assert models_named_in(bikes, "HONDA CBR150R 2018 Blue Card") == ("cbr",)
    assert models_named_in(bikes, "🔥 HONDA CB1000R 🔥") == ("cb",)


def test_a_cbr_lot_stuck_to_its_displacement_is_kept_on_a_cbr_query() -> None:
    """«honda cbr» обязан ОТДАВАТЬ CBR-лоты, а не отрезать их.

    Имя в объявлении слипается с объёмом («CBR150R»), и целое слово `\\bcbr\\b`
    его бы не увидело — лот с настоящим CBR отсеялся бы как «чужая модель». Регекс
    по префиксу это чинит: живой отказ — «honda cbr» возвращал Honda PSX, потому
    что CBR не узнавался ни в запросе, ни в лоте.
    """
    # Без бюджета: спортбайк дороже скутера, и потолок в 500 USD отсёк бы CBR
    # ценой раньше, чем проверится модель. Здесь проверяется именно модель.
    cbr = Passport(
        intent=Intent.BUY,
        category=Category.MOTORBIKE,
        city=CITY,
        attributes={"model": "cbr", "brand": "honda", "transmission": "manual"},
        raw_query="honda cbr",
    )
    real = lot("HONDA CBR150R 2018 Blue Card, механика", price=33_000_000)
    other = lot("Продам Honda PSX 2011", price=17_000_000)

    assert shown(cbr, [real, other]) == [real.title]


def test_the_z_letter_needs_its_brand_and_ignores_kenzo_z20() -> None:
    """Одиночная «z» ловится в чужих словах, и это не гипотеза, а живой лот.

    На снимке Нячанга «дополнительный свет Kenzo Z20» дал бы ложное «z» голым
    правилом `z\\d`, и такой лот прошёл бы фильтр на запрос «kawasaki z». Поэтому
    «z» — только с якорем по марке: «kawasaki z300» узнаётся, «Kenzo Z20» — нет.
    Безмарочный «Z1000» при этом осознанно пропущен — пропуск оставляет выдачу как
    была, ложная модель режет верную (дисциплина «candy»/«SH»).
    """
    bikes = Category.MOTORBIKE

    assert models_named_in(bikes, "Kawasaki Z1000 2018, срочно") == ("z",)
    assert "z" not in models_named_in(bikes, "фара Kenzo Z20 противотуманная")
    assert "z" not in models_named_in(bikes, "Yamaha Sirius, свет Kenzo Z20")


def test_monster_needs_ducati_or_a_displacement_not_a_sticker() -> None:
    """«monster» — обычное слово: наклейка «Monster Energy» на чужом байке не модель.

    Якорь двойной — «ducati monster» либо «monster» + объём («monster 797»);
    голое «Monster Energy» не проходит ни то, ни другое, и честный Honda Winner с
    такой наклейкой своей моделью и остаётся.
    """
    bikes = Category.MOTORBIKE

    assert models_named_in(bikes, "Ducati Monster 797 2019") == ("monster",)
    assert "monster" not in models_named_in(bikes, "Honda Winner, наклейки Monster Energy")


@pytest.mark.parametrize("text", ["продам mt", "куплю rc", "нужен xr", "honda cb недорого"])
def test_short_family_names_do_not_fire_without_their_anchor(text: str) -> None:
    """Короткое имя без цифры объёма и без марки-якоря — не модель.

    Голые «mt», «rc», «xr», «cb» встречаются в тексте случайно, поэтому вносятся
    ТОЛЬКО с якорем. Без него разбор возвращает прежнее поведение (модель не
    названа), а не ловит лишнее: ложная модель отрезала бы верную выдачу.
    """
    assert detect_model(text, Category.MOTORBIKE) is None


def test_ktm_is_a_new_brand_that_names_the_category() -> None:
    """KTM — новая марка рынка: «ktm» без иных слов это мотобайк, как «yamaha».

    До этого KTM в `MOTORBIKE_BRANDS` не было, и «ktm» оставалась без категории —
    тот же дефект, что был у «yamaha» (realcheck 02.09.2026).
    """
    assert parse_query("ktm до 5000", default_city=CITY).category is Category.MOTORBIKE
    assert parse_query("ktm до 5000", default_city=CITY).attributes["brand"] == "ktm"


def test_a_bare_sport_family_lot_is_dropped_on_a_housing_query() -> None:
    """Категория следует из имени семейства и на стороне лота: Ninja не жильё.

    «KAWASAKI NINJA 400» слова «мотоцикл» не содержит, но Ninja бывает лишь у
    байка — тот же вывод, что спас выдачу студий от Honda Lead и Yamaha R15.
    """
    wants_flat = Passport(
        intent=Intent.RENT,
        category=Category.APARTMENT,
        city=CITY,
        raw_query="сниму студию у моря",
    )
    bare_bike = lot("KAWASAKI NINJA 400 — продажа, Нячанг")
    flat = lot("Студия с мебелью, длительный срок")

    assert shown(wants_flat, [bare_bike, flat]) == [flat.title]


@pytest.mark.parametrize(
    "model",
    [
        "cbr",
        "cb",
        "rebel",
        "crf",
        "xr",
        "mt",
        "r3",
        "r25",
        "fz",
        "ninja",
        "z",
        "zx",
        "gsx",
        "raider",
        "duke",
        "rc",
        "monster",
        "panigale",
    ],
)
def test_a_sport_family_carries_no_displacement(model: str) -> None:
    """Объём семейства стоит в имени и РАЗНЫЙ — одно число соврало бы.

    Поэтому `engine_cc=None` (как у электро, но по другой причине): настоящий объём
    берётся из числа лота (`relevance._model_cc_values` вернёт пусто, и лот держит
    «неизвестное ≠ несовпадение», а не отсекается выдуманным классом).
    """
    assert model_engine_cc(model) is None
