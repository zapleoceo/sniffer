"""Словарь рынка и структурные фильтры: где кончаются слова и начинаются поля.

Проверяется не «функция вернула список», а поведение, из-за которого выдача
была мусорной: на «нужен скутер в нячанге» приезжал Honda Winner X 2021 —
спортбайк с механикой — потому что план уходил общим запросом по категории.

И второе поведение, из-за которого выдача была ПУСТОЙ: слово о свойстве в `q`
структурной доски гасит верный структурный фильтр, потому что доска складывает
их через И. Замер: `motorbiketype=3` — 12 объявлений, он же с `q='côn tay'` —
ноль. Клиент читает пустоту как «на рынке нет механики», хотя её двенадцать.
Первый баг был виден глазами, второй — нет, поэтому здесь он закреплён по
КАЖДОМУ значению атрибута из docs/passport.md, а не по одному.

Живой сети здесь нет. Числа из замеров (Нячанг, cg=2020, 31.08.2026) лежат в
`market_terms.BOARD_QUERY_HITS` и в docs/spec-v2.md: тест, который ходит на
Chotot, падает не когда сломан код, а когда Chotot чихнул.
"""

from __future__ import annotations

from typing import Any

import pytest

from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport, PricePeriod
from sniffer.search.fallback import fallback_plan
from sniffer.search.market_terms import (
    BOARD_ATTRIBUTE_TERMS,
    BOARD_QUERY_HITS,
    BOARD_QUERY_TOTAL,
)
from sniffer.search.plan import MAX_TASKS, SearchPlan, context_params, parse_tasks
from sniffer.search.vocabulary import (
    accepts_jargon,
    attribute_phrases,
    board_attribute_phrases,
    category_terms,
    is_board_safe,
    source_langs,
    wants_city_in_query,
)
from sniffer.sources.chotot import build_params
from sniffer.sources.chotot_filters import attribute_params, budget_params
from sniffer.sources.chotot_reference import (
    MOTORBIKE_TYPE_AUTOMATIC,
    MOTORBIKE_TYPE_FOOTSHIFT,
    MOTORBIKE_TYPE_MANUAL,
)

SOURCES = ["telegram_groups", "chotot"]
WEB_SOURCES = ["telegram_groups", "chotot", "web"]

# Все значения атрибутов мотобайка из docs/passport.md. Одно значение в тестах
# ловит ровно один баг: «tay ga» безопасно как `q` только потому, что случайно
# совпадает с motorbiketype=1, и на `transmission=automatic` дефект незаметен.
MOTORBIKE_ATTRIBUTES: dict[str, tuple[Any, ...]] = {
    "transmission": ("automatic", "manual", "semi"),
    "condition": ("new", "good", "worn"),
    "papers": ("blue_card", "none"),
    "engine_cc": (50, 125, 400),
    "year_min": (2018, 2024),
    "brand": ("Honda", "Vespa", "Zongshen"),
    "delivery": (True, False),
    "test_ride": (True, False),
}
ATTRIBUTE_CASES = [
    pytest.param({attribute: value}, id=f"{attribute}={value}")
    for attribute, values in MOTORBIKE_ATTRIBUTES.items()
    for value in values
]
# Пары: фильтр по одному атрибуту не должен гаситься словом другого.
ATTRIBUTE_PAIRS = [
    pytest.param({"transmission": "manual", "papers": "blue_card"}, id="manual+blue_card"),
    pytest.param({"transmission": "semi", "condition": "good"}, id="semi+good"),
    pytest.param({"transmission": "automatic", "condition": "new"}, id="automatic+new"),
    pytest.param({"brand": "Vespa", "condition": "worn"}, id="vespa+worn"),
]

# Атрибуты, которые Chotot отбирает своим полем: они едут фильтром и словом не
# дублируются (замер: `q='honda'` и `motorbikebrand=1` дают одни и те же 26).
FILTERED_ATTRIBUTES = ("transmission", "engine_cc", "year_min", "brand")

# Слова, которые замер дисквалифицировал как `q` доски. Ноль лжёт клиенту,
# полная выдача не фильтрует ничего (spec-v2 2.2, правила 4 и 5).
BOARD_ZERO_WORDS = (
    "xe ga",
    "côn tay",
    "xe côn tay",
    "bán tự động",
    "cà vẹt",
    "cavet",
    "giấy tờ đầy đủ",
    "xe cũ",
    "bán nguyên zin",
    "bán cavet",
    "bán xe mới",
)
BOARD_FULL_OUTPUT_WORDS = ("xe máy", "chính chủ", "nguyên zin")


def make_passport(**overrides: Any) -> Passport:
    fields: dict[str, Any] = {
        "intent": Intent.BUY,
        "category": Category.MOTORBIKE,
        "city": "nha_trang",
        "budget": Budget(max=400, currency=Currency.USD, period=PricePeriod.ONCE),
        "attributes": {"transmission": "automatic"},
        "raw_query": "нужен скутер в нячанге",
    }
    fields.update(overrides)
    return Passport(**fields)


def queries(plan: SearchPlan, source: str | None = None) -> list[str]:
    return [task.query for task in plan.tasks if source is None or task.source == source]


def board_params(attributes: dict[str, Any]) -> list[dict[str, Any]]:
    """Что уедет на Chotot полным путём: паспорт → фолбэк → build_params."""
    plan = fallback_plan(make_passport(attributes=attributes), ["chotot"], reason="тест")
    return [build_params(task.query, task.params) for task in plan.tasks]


# --------------------------------------------------------------------------
# 1. Расширение запроса по категории и по атрибутам
# --------------------------------------------------------------------------


def test_query_expands_beyond_client_words() -> None:
    """Клиент сказал «скутер» — искать надо и тем, чего он не говорил."""
    plan = fallback_plan(make_passport(), WEB_SOURCES, reason="тест")
    text = " | ".join(queries(plan))

    assert "скутер" in text  # слово клиента осталось
    assert "автомат" in text  # свойство названо словом рынка
    assert "инжектор" in text  # жаргон, которого клиент не знает
    assert "tay ga" in text  # перевод на язык рынка


def test_attribute_becomes_a_market_word_not_a_passport_key() -> None:
    """`transmission=automatic` в тексте звучит как «автомат», а не как ключ."""
    assert attribute_phrases(Category.MOTORBIKE, {"transmission": "automatic"}, "ru") == [
        "автомат",
        "вариатор",
    ]
    assert attribute_phrases(Category.MOTORBIKE, {"transmission": "manual"}, "vi") == [
        "côn tay",
        "xe côn tay",
    ]
    # Незнакомое значение молча пропускается: «коробка неважна» — не ошибка.
    assert attribute_phrases(Category.MOTORBIKE, {"transmission": "нужен любой"}, "ru") == []


def test_vietnamese_noun_is_the_one_that_finds_something() -> None:
    """Замер: «tay ga» — 41 объявление из 59, «xe ga» — ноль.

    «xe ga» стояло в словаре первым и не находило НИЧЕГО. Регрессия дорогая и
    невидимая (пустая выдача выглядит как «на рынке нет»), поэтому закреплено.
    """
    vietnamese = category_terms(Category.MOTORBIKE, "vi")

    assert vietnamese[0] == "tay ga"
    assert "xe ga" not in vietnamese


def test_category_is_data_not_branching() -> None:
    """Новая категория — строка в таблице, а не ветка в коде."""
    plan = fallback_plan(
        make_passport(category=Category.APARTMENT, intent=Intent.RENT, attributes={}),
        WEB_SOURCES,
        reason="тест",
    )
    text = " | ".join(queries(plan))

    assert "квартира" in text
    assert "căn hộ" in text
    assert "скутер" not in text


def test_papers_words_live_in_one_table() -> None:
    """Слова документов нужны и жаргону, и атрибуту `papers` — знание одно.

    Две копии «блюкарт» однажды разъедутся, и правку внесут только в одну.
    """
    from sniffer.search.market_terms import ATTRIBUTE_TERMS, JARGON, PAPERS_WORDS

    assert ATTRIBUTE_TERMS[Category.MOTORBIKE]["papers"]["blue_card"] is PAPERS_WORDS
    for lang, words in PAPERS_WORDS.items():
        assert set(words) <= set(JARGON[Category.MOTORBIKE][lang])


# --------------------------------------------------------------------------
# 2. Язык выбирается по источнику, а не по языку клиента
# --------------------------------------------------------------------------


def test_language_follows_the_source() -> None:
    """Русский запрос к вьетнамской доске вернёт ноль, и наоборот."""
    plan = fallback_plan(make_passport(), SOURCES, reason="тест")

    chotot_langs = {task.lang for task in plan.tasks if task.source == "chotot"}
    telegram_langs = {task.lang for task in plan.tasks if task.source == "telegram_groups"}

    assert chotot_langs == {"vi"}
    assert "vi" not in telegram_langs
    assert telegram_langs <= {"ru", "en"}


def test_client_language_does_not_leak_into_the_plan() -> None:
    """Формулировка по-русски не делает план русским: язык берётся у источника."""
    plan = fallback_plan(
        make_passport(raw_query="СРОЧНО нужен скутер, пишу по-русски"),
        SOURCES,
        reason="тест",
    )

    assert all(task.lang == "vi" for task in plan.tasks if task.source == "chotot")


def test_unknown_source_gets_every_market_language() -> None:
    """Адаптер без профиля работает вслепую, но работает."""
    assert source_langs("baraholka_2026") == ("ru", "vi", "en")
    assert accepts_jargon("baraholka_2026")
    assert wants_city_in_query("baraholka_2026")


# --------------------------------------------------------------------------
# 3. Город — только тем источникам, что ищут по всему интернету
# --------------------------------------------------------------------------


def test_city_only_in_queries_of_whole_internet_sources() -> None:
    """В чате Нячанга слово «Нячанг» в объявлениях не пишут."""
    plan = fallback_plan(make_passport(), WEB_SOURCES, reason="тест")

    assert any("Нячанг" in query or "Nha Trang" in query for query in queries(plan, "web"))
    assert all("Нячанг" not in query for query in queries(plan, "telegram_groups"))
    assert all("Nha Trang" not in query for query in queries(plan, "chotot"))


def test_city_always_reaches_the_adapter_as_a_parameter() -> None:
    """Не в тексте — значит параметром, иначе адаптер не знает, где искать."""
    plan = fallback_plan(make_passport(), WEB_SOURCES, reason="тест")

    assert all(task.params["city"] == "nha_trang" for task in plan.tasks)


# --------------------------------------------------------------------------
# 4. Жаргон — источникам со свободным текстом, и только им
# --------------------------------------------------------------------------


def test_jargon_goes_to_telegram_and_not_to_chotot() -> None:
    """Замер по Chotot: «скутер» 0, «инжектор» 0, «блюкарт» 0 объявлений.

    Задача жаргоном к структурной доске — впустую потраченный запрос из
    бюджета плана (12 задач на весь план, spec-v2 2.3).
    """
    plan = fallback_plan(make_passport(), SOURCES, reason="тест")

    telegram = queries(plan, "telegram_groups")
    chotot = " | ".join(queries(plan, "chotot"))

    assert "инжектор" in telegram
    assert "блюкарт" in telegram
    assert "инжектор" not in chotot
    assert "блюкарт" not in chotot


def test_jargon_is_a_separate_query_not_a_suffix() -> None:
    """Смысл жаргона в том, что предмет продавец вообще не назвал."""
    plan = fallback_plan(make_passport(), SOURCES, reason="тест")

    assert "инжектор" in queries(plan, "telegram_groups")


@pytest.mark.parametrize("attributes", ATTRIBUTE_CASES)
def test_prose_source_still_describes_the_property_by_word(attributes: dict[str, Any]) -> None:
    """У чата фасет нет, поэтому свойство там обязано остаться словом.

    Обратная сторона починки: запретив слова атрибутов доске, легко случайно
    отнять их и у чата, где они единственный инструмент.
    """
    words = attribute_phrases(Category.MOTORBIKE, attributes, "ru")
    plan = fallback_plan(make_passport(attributes=attributes), ["telegram_groups"], reason="тест")
    text = " | ".join(queries(plan, "telegram_groups"))

    assert plan.tasks
    assert all(word in text for word in words[:1])


# --------------------------------------------------------------------------
# 5. Структурные поля Chotot — в params, а не в текст запроса
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("transmission", "expected"),
    [
        ("automatic", MOTORBIKE_TYPE_AUTOMATIC),
        ("semi", MOTORBIKE_TYPE_FOOTSHIFT),
        ("manual", MOTORBIKE_TYPE_MANUAL),
    ],
)
def test_transmission_becomes_a_structural_filter(transmission: str, expected: int) -> None:
    """Замер: общий запрос — 71% скутеров-автоматов, motorbiketype=1 — 100%."""
    built = attribute_params("motorbike", {"transmission": transmission})

    assert built == {"motorbiketype": expected}


def test_structural_fields_land_in_params_not_in_the_query_text() -> None:
    """Тип кузова — поле, а не слово: он отсекает механику надёжнее текста."""
    built = board_params({"transmission": "automatic"})[0]

    assert built["motorbiketype"] == MOTORBIKE_TYPE_AUTOMATIC
    assert built["cg"] == 2020
    assert built["region_v2"] == 7044
    assert built["area_v2"] == 704401
    # Ни коробки, ни бюджета, ни вообще слова: `q` складывается с фильтром через
    # И, и «tay ga» с motorbiketype=1 даёт те же 41 — уточнять нечего, а с
    # motorbiketype=3 то же слово даёт ноль вместо 12.
    assert "q" not in built


def test_every_supported_attribute_reaches_chotot_as_a_field() -> None:
    built = attribute_params(
        Category.MOTORBIKE,
        {"transmission": "automatic", "brand": "Vespa", "year_min": 2018, "engine_cc": 125},
    )

    assert built["motorbiketype"] == MOTORBIKE_TYPE_AUTOMATIC
    assert built["motorbikebrand"] == 3  # Vespa — марка Piaggio, тот же код
    assert built["regdate"] == 2018  # семантика «не раньше», проверено замером
    assert built["motorbikecapacity"] == 3  # корзина 100–175 cc


@pytest.mark.parametrize(
    ("cc", "bucket"),
    [(50, 1), (49, 1), (100, 2), (110, 3), (125, 3), (175, 3), (176, 4), (400, 4)],
)
def test_engine_capacity_buckets(cc: int, bucket: int) -> None:
    """Границы корзин восстановлены по «NNcc» в тексте 100 объявлений каждой."""
    assert attribute_params(Category.MOTORBIKE, {"engine_cc": cc}) == {"motorbikecapacity": bucket}


def test_attributes_of_a_foreign_category_do_not_leak() -> None:
    """`motorbiketype` существует только у cg=2020: в квартирах это гарантированный ноль."""
    assert attribute_params(Category.APARTMENT, {"transmission": "automatic"}) == {}
    assert attribute_params("не категория", {"transmission": "automatic"}) == {}


def test_unknown_attribute_is_ignored_not_fatal() -> None:
    built = attribute_params(Category.MOTORBIKE, {"цвет": "синий", "transmission": "automatic"})

    assert built == {"motorbiketype": MOTORBIKE_TYPE_AUTOMATIC}


def test_budget_filters_only_in_dong() -> None:
    """Курс USD→VND не выдумывается: захардкоженный протухает молча."""
    assert budget_params({"max": 25_000_000, "currency": "VND"}) == {"price": "0-25000000"}
    assert budget_params({"min": 5_000_000, "max": 25_000_000, "currency": "VND"}) == {
        "price": "5000000-25000000"
    }
    assert budget_params({"max": 400, "currency": "USD"}) == {}
    assert budget_params({"max": 400}) == {}
    # Перевёрнутый диапазон Chotot принял бы и вернул пустоту — не отправляем.
    assert budget_params({"min": 30_000_000, "max": 10_000_000, "currency": "VND"}) == {}


def test_plan_filter_beats_the_attribute_translation() -> None:
    """Модель могла узнать про источник то, чего в переводчике ещё нет."""
    params = {
        "category": "motorbike",
        "attributes": {"transmission": "automatic"},
        "motorbiketype": "1,3",  # многозначная форма, замер: 53 из 59
    }

    assert build_params("tay ga", params)["motorbiketype"] == "1,3"


# --------------------------------------------------------------------------
# 6. `q` структурной доски: только измеренное, иначе фильтр гаснет в ноль
# --------------------------------------------------------------------------


@pytest.mark.parametrize("attributes", ATTRIBUTE_CASES + ATTRIBUTE_PAIRS)
def test_no_unmeasured_word_reaches_the_board(attributes: dict[str, Any]) -> None:
    """Каждое слово в `q` доски обязано иметь замер, и замер не ноль.

    Это главный инвариант: слово, попавшее в `q` без числа, — это либо пустая
    выдача, либо задача из бюджета плана, потраченная на фильтр, который ничего
    не отфильтровал.
    """
    for built in board_params(attributes):
        query = built.get("q")
        assert query is None or is_board_safe(query), f"неизмеренное слово в q: {query!r}"


@pytest.mark.parametrize("attributes", ATTRIBUTE_CASES + ATTRIBUTE_PAIRS)
def test_filtered_attribute_is_not_duplicated_by_a_word(attributes: dict[str, Any]) -> None:
    """Есть поле — едет поле. Слово о том же свойстве только гасит его.

    Замер: `motorbiketype=3` — 12 объявлений, с `q='côn tay'` — ноль;
    `motorbiketype=2` — 5, с `q='bán tự động'` — ноль.
    """
    for built in board_params(attributes):
        for attribute in FILTERED_ATTRIBUTES:
            if attribute not in attributes:
                continue
            words = attribute_phrases(Category.MOTORBIKE, {attribute: attributes[attribute]}, "vi")
            assert all(word not in built.get("q", "") for word in words)


@pytest.mark.parametrize("attributes", ATTRIBUTE_CASES + ATTRIBUTE_PAIRS)
def test_word_measured_as_zero_never_reaches_the_board(attributes: dict[str, Any]) -> None:
    """Регрессия на измеренные нули: «côn tay», «cà vẹt», «xe cũ», «xe ga».

    Каждое из них — правильное слово рынка и правильный запрос к чату. К доске
    это ноль объявлений, то есть ложь клиенту «на рынке нет».
    """
    for built in board_params(attributes):
        query = built.get("q", "")
        assert all(word not in query for word in BOARD_ZERO_WORDS)


@pytest.mark.parametrize("attributes", ATTRIBUTE_CASES + ATTRIBUTE_PAIRS)
def test_word_that_filters_nothing_never_reaches_the_board(attributes: dict[str, Any]) -> None:
    """«chính chủ» отдаёт все 59 из 59 — spec-v2 2.2 правило 5: это не фильтр.

    Слово, не сужающее выдачу, тратит задачу из бюджета плана и создаёт вид
    работающего поиска.
    """
    for built in board_params(attributes):
        query = built.get("q", "")
        assert all(word not in query for word in BOARD_FULL_OUTPUT_WORDS)


@pytest.mark.parametrize("attributes", ATTRIBUTE_CASES + ATTRIBUTE_PAIRS)
def test_deal_verb_is_not_glued_to_the_board_query(attributes: dict[str, Any]) -> None:
    """Глагол сделки приставкой — приём прозы; доске он ломает даже рабочее слово.

    Замер: «nguyên zin» 59 → «bán nguyên zin» 0; «xe mới» 8 → «bán xe mới» 0.
    """
    for built in board_params(attributes):
        assert not built.get("q", "").startswith("bán")


@pytest.mark.parametrize("attributes", ATTRIBUTE_CASES + ATTRIBUTE_PAIRS)
def test_board_always_gets_exactly_one_task_with_its_filters(attributes: dict[str, Any]) -> None:
    """Источник не должен выпасть из плана из-за того, что слов для него нет."""
    plan = fallback_plan(make_passport(attributes=attributes), ["chotot"], reason="тест")

    assert plan.sources() == ["chotot"]
    assert len(plan.tasks) == 1
    assert build_params(plan.tasks[0].query, plan.tasks[0].params)["cg"] == 2020


def test_attribute_without_a_filter_gives_the_honest_full_output() -> None:
    """`papers` Chotot полем не отбирает — значит не отбираем вовсе.

    Замер: `cà vẹt`, `cavet`, `giấy tờ đầy đủ` — ноль каждое при 59 без запроса.
    Лучше 59 честных объявлений и ранжирование, чем пустота с красивым словом.
    """
    built = board_params({"papers": "blue_card"})[0]

    assert "q" not in built
    assert not any(key.startswith("motorbike") for key in built)


def test_measured_attribute_word_does_reach_the_board() -> None:
    """Whitelist не пустой лозунг: «xe mới» замер прошло (8 из 59) и едет.

    Если бы правило было «доске никогда никаких слов», этот тест был бы красным,
    и заодно он держит живым сам путь board-слова в `q`.
    """
    built = board_params({"condition": "new"})[0]

    assert built["q"] == "xe mới"
    assert board_attribute_phrases(Category.MOTORBIKE, {"condition": "new"}, "vi") == ["xe mới"]


def test_board_whitelist_is_backed_by_a_measurement() -> None:
    """spec-v2 2.2 правила 4 и 5 распространяются и на ATTRIBUTE_TERMS.

    Слово без числа, слово с нулём и слово, отдающее всю выдачу, в whitelist
    попасть не могут — иначе таблица снова станет догадкой.
    """
    for attribute_values in BOARD_ATTRIBUTE_TERMS.values():
        for values in attribute_values.values():
            for langs in values.values():
                for terms in langs.values():
                    for term in terms:
                        assert term in BOARD_QUERY_HITS, f"{term} без замера"
                        assert 0 < BOARD_QUERY_HITS[term] < BOARD_QUERY_TOTAL


@pytest.mark.parametrize("word", BOARD_ZERO_WORDS)
def test_measured_zeros_stay_recorded_as_zero(word: str) -> None:
    """Числа замера — данные, и они не должны молча «улучшиться»."""
    assert BOARD_QUERY_HITS[word] == 0
    assert not is_board_safe(word)


@pytest.mark.parametrize("word", BOARD_FULL_OUTPUT_WORDS)
def test_words_that_filter_nothing_stay_recorded_as_full_output(word: str) -> None:
    assert BOARD_QUERY_HITS[word] == BOARD_QUERY_TOTAL
    assert not is_board_safe(word)


def test_unmeasured_word_is_not_board_safe() -> None:
    """Отсутствие замера — это «нельзя», а не «наверное можно»."""
    assert not is_board_safe("xe máy độ kiểu")
    assert not is_board_safe("скутер")


# --------------------------------------------------------------------------
# 7. Фолбэк проходит ту же нормализацию, что и ответ модели
# --------------------------------------------------------------------------


@pytest.mark.parametrize("attributes", ATTRIBUTE_CASES + ATTRIBUTE_PAIRS)
def test_fallback_and_model_plan_build_identical_requests(attributes: dict[str, Any]) -> None:
    """spec-v2 2.4: иначе фолбэк разъедется с боевым путём.

    Сравнивается не `.params`, а результат `build_params()` — именно там
    расходился `q`, и сравнение только параметров этого не видело.
    """
    passport = make_passport(attributes=attributes)
    model_tasks = parse_tasks(
        [{"source": "chotot", "query": "", "lang": "vi", "params": [], "priority": 1}],
        SOURCES,
    )
    model_plan = SearchPlan.from_tasks(model_tasks, defaults=context_params(passport))
    fallback = fallback_plan(passport, SOURCES, reason="тест")

    chotot_model = next(task for task in model_plan.tasks if task.source == "chotot")
    chotot_fallback = next(task for task in fallback.tasks if task.source == "chotot")

    assert chotot_model.params == chotot_fallback.params
    model_request = build_params(chotot_model.query, chotot_model.params)
    # Модель прислала пустой текст, фолбэк мог добавить измеренное слово —
    # структурная часть запроса обязана совпасть до последнего поля.
    fallback_request = build_params(chotot_fallback.query, chotot_fallback.params)
    assert model_request == {key: value for key, value in fallback_request.items() if key != "q"}


def test_empty_query_is_legal_for_a_board_and_dropped_for_a_chat() -> None:
    """Пустой `q` — лучший запрос к доске и бесполезная задача для чата."""
    tasks = parse_tasks(
        [
            {"source": "chotot", "query": "", "lang": "vi", "params": [], "priority": 1},
            {"source": "telegram_groups", "query": "  ", "lang": "ru", "params": [], "priority": 1},
        ],
        SOURCES,
    )

    assert [task.source for task in tasks] == ["chotot"]


def test_fallback_respects_the_plan_budget_and_dedups() -> None:
    plan = fallback_plan(make_passport(), WEB_SOURCES, reason="тест")
    keys = [task.dedup_key() for task in plan.tasks]

    assert len(plan.tasks) <= MAX_TASKS
    assert len(keys) == len(set(keys))
    assert plan.is_fallback
    # Обрезка по приоритету, а не по порядку: главный запрос каждого источника
    # обязан выжить, иначе источник выпадает из плана целиком.
    assert set(plan.sources()) == set(WEB_SOURCES)


def test_fallback_marks_itself_and_names_the_reason() -> None:
    """Доля фолбэков — операционная метрика: по ней видно, что брокер лежит."""
    plan = fallback_plan(make_passport(), SOURCES, reason="BrokerCapError")

    assert plan.is_fallback
    assert "BrokerCapError" in plan.reasoning


def test_context_params_hands_over_neutral_facts_only() -> None:
    """Планировщик не знает слова `motorbiketype` — перевод делает адаптер."""
    params = context_params(make_passport())

    assert params["attributes"] == {"transmission": "automatic"}
    assert params["budget"]["currency"] == "USD"
    assert not any(key.startswith("motorbike") for key in params)


# --------------------------------------------------------------------------
# 8. Промпт не противоречит сам себе
# --------------------------------------------------------------------------


def test_prompt_shows_property_words_only_to_prose_sources() -> None:
    """Запрет писать свойства в текст и список этих слов рядом — тот же баг.

    Модель прочитает список и напишет «côn tay» в `q` доски, а это ноль.
    """
    from sniffer.search.prompt import build_user_prompt

    passport = make_passport(attributes={"transmission": "manual"})
    prose_only = build_user_prompt(passport, ["web"])
    board_only = build_user_prompt(passport, ["chotot"])

    assert "côn tay" in prose_only
    assert "côn tay" not in board_only
    assert "query пустым" in board_only


def test_prompt_offers_the_board_only_measured_words() -> None:
    from sniffer.search.prompt import build_user_prompt

    board_only = build_user_prompt(make_passport(attributes={"condition": "new"}), ["chotot"])

    assert "xe mới" in board_only
    assert "nguyên zin" not in board_only
