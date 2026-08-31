"""Словарь рынка и структурные фильтры: где кончаются слова и начинаются поля.

Проверяется не «функция вернула список», а поведение, из-за которого выдача
была мусорной: на «нужен скутер в нячанге» приезжал Honda Winner X 2021 —
спортбайк с механикой — потому что план уходил общим запросом по категории.

Живой сети здесь нет. Числа из замеров (Нячанг, cg=2020, 31.08.2026) лежат в
комментариях и в docs/spec-v2.md: тест, который ходит на Chotot, падает не
когда сломан код, а когда Chotot чихнул.
"""

from __future__ import annotations

from typing import Any

import pytest

from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport, PricePeriod
from sniffer.search.fallback import fallback_plan
from sniffer.search.plan import MAX_TASKS, SearchPlan, context_params, parse_tasks
from sniffer.search.vocabulary import (
    accepts_jargon,
    attribute_phrases,
    category_terms,
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


# --------------------------------------------------------------------------
# 1. Расширение запроса по категории и по атрибутам
# --------------------------------------------------------------------------


def test_query_expands_beyond_client_words() -> None:
    """Клиент сказал «скутер» — искать надо и тем, чего он не говорил."""
    plan = fallback_plan(make_passport(), SOURCES, reason="тест")
    text = " | ".join(queries(plan))

    assert "скутер" in text  # слово клиента осталось
    assert "автомат" in text  # свойство названо словом рынка
    assert "инжектор" in text  # жаргон, которого клиент не знает
    assert "tay ga" in text  # перевод на язык доски


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
        SOURCES,
        reason="тест",
    )
    text = " | ".join(queries(plan))

    assert "квартира" in text
    assert "căn hộ" in text
    assert "скутер" not in text


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
    plan = fallback_plan(make_passport(), ["chotot"], reason="тест")
    task = plan.tasks[0]
    built = build_params(task.query, task.params)

    assert built["motorbiketype"] == MOTORBIKE_TYPE_AUTOMATIC
    assert built["cg"] == 2020
    assert built["region_v2"] == 7044
    assert built["area_v2"] == 704401
    # Ни коробки, ни бюджета в тексте: Chotot ищет по `q` непредсказуемо, а по
    # полю — точно. «automatic» в `q` попросту вернул бы ноль.
    assert "automatic" not in built["q"]
    assert "motorbiketype" not in built["q"]


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
# 6. Фолбэк проходит ту же нормализацию, что и ответ модели
# --------------------------------------------------------------------------


def test_fallback_and_model_plan_carry_identical_context() -> None:
    """spec-v2 2.4: иначе фолбэк разъедется с боевым путём."""
    passport = make_passport()
    model_tasks = parse_tasks(
        [{"source": "chotot", "query": "tay ga", "lang": "vi", "params": [], "priority": 1}],
        SOURCES,
    )
    model_plan = SearchPlan.from_tasks(model_tasks, defaults=context_params(passport))
    fallback = fallback_plan(passport, SOURCES, reason="тест")

    chotot_model = next(task for task in model_plan.tasks if task.source == "chotot")
    chotot_fallback = next(task for task in fallback.tasks if task.source == "chotot")

    assert chotot_model.params == chotot_fallback.params
    # И, как следствие, один и тот же структурный фильтр на выходе адаптера.
    assert (
        build_params(chotot_model.query, chotot_model.params)["motorbiketype"]
        == (build_params(chotot_fallback.query, chotot_fallback.params)["motorbiketype"])
    )


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
