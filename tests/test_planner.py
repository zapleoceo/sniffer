"""Планировщик — место, где ответ модели встречается с бюджетом поиска.

Модель здесь замокана: тест, который ходит в брокер, не тест. Проверяется не
только удачный путь, но и каждый способ, которым модель может подвести, —
именно ради них существует шаблонный план.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from sniffer.broker.client import BrokerCapError, BrokerError
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport, PricePeriod
from sniffer.search.fallback import fallback_plan
from sniffer.search.plan import MAX_TASKS, SearchPlan, SearchTask
from sniffer.search.planner import SearchPlanner
from sniffer.search.prompt import SYSTEM_PROMPT, build_user_prompt, plan_schema

SOURCES = ["telegram_groups", "chotot"]

GOOD_PAYLOAD: dict[str, Any] = {
    "tasks": [
        {
            "source": "telegram_groups",
            "query": "инжектор",
            "lang": "ru",
            "params": [],
            "priority": 1,
        },
        {
            "source": "telegram_groups",
            "query": "блюкарт",
            "lang": "ru",
            "params": [],
            "priority": 2,
        },
        {
            "source": "chotot",
            "query": "xe ga",
            "lang": "vi",
            "params": [{"key": "cg", "value": "2020"}],
            "priority": 1,
        },
    ],
    "reasoning": "вьетнамский сегмент в TG отсутствует, локальный рынок берём с Chotot",
}


def make_passport(**overrides: Any) -> Passport:
    fields: dict[str, Any] = {
        "intent": Intent.BUY,
        "category": Category.MOTORBIKE,
        "city": "nha_trang",
        "budget": Budget(max=400, currency=Currency.USD, period=PricePeriod.ONCE),
        "attributes": {"transmission": "automatic"},
        "raw_query": "ищу скутер до 400 долларов",
    }
    fields.update(overrides)
    return Passport(**fields)


class FakeBroker:
    """Подменяет только `structured` — больше планировщику от брокера не нужно."""

    def __init__(self, payload: Any = None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def structured(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        schema_name: str,
        system: str | None = None,
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        self.calls.append({"prompt": prompt, "schema": schema, "system": system})
        if self.error is not None:
            raise self.error
        return dict(self.payload) if isinstance(self.payload, dict) else self.payload


async def test_valid_answer_becomes_plan() -> None:
    broker = FakeBroker(GOOD_PAYLOAD)

    plan = await SearchPlanner(broker).plan(make_passport(), SOURCES)

    assert not plan.is_fallback
    # Порядок задач — по приоритету, а не по порядку перечисления моделью.
    assert [task.query for task in plan.tasks] == ["инжектор", "xe ga", "блюкарт"]
    assert plan.reasoning.startswith("вьетнамский сегмент")
    assert len(broker.calls) == 1  # бюджет spec-v2 2.3: один вызов LLM на план


async def test_params_reach_the_adapter() -> None:
    plan = await SearchPlanner(FakeBroker(GOOD_PAYLOAD)).plan(make_passport(), SOURCES)
    chotot = next(task for task in plan.tasks if task.source == "chotot")

    # Пары строк из strict-схемы разворачиваются в параметры источника, число
    # остаётся числом: Chotot ждёт cg=2020, а не "2020".
    assert chotot.params["cg"] == 2020
    # Город и категория доезжают до адаптера, который паспорта не видит.
    assert chotot.params["city"] == "nha_trang"
    assert chotot.params["category"] == "motorbike"


async def test_plan_is_cut_to_budget() -> None:
    noise: list[dict[str, Any]] = [
        {
            "source": "telegram_groups",
            "query": f"honda модель {index}",
            "lang": "ru",
            "params": [],
            "priority": 3,
        }
        for index in range(20)
    ]
    payload = {
        "tasks": [*noise, {"source": "chotot", "query": "xe ga", "lang": "vi", "priority": 1}],
        "reasoning": "",
    }

    plan = await SearchPlanner(FakeBroker(payload)).plan(make_passport(), SOURCES)

    assert len(plan.tasks) == MAX_TASKS
    assert not plan.is_fallback
    # Обрезали по приоритету: догадки ушли, очевидное осталось.
    assert plan.tasks[0].query == "xe ga"


def test_plan_model_refuses_more_than_budget() -> None:
    tasks = [SearchTask(source="telegram_groups", query=f"q{i}") for i in range(MAX_TASKS + 1)]

    with pytest.raises(ValidationError):
        SearchPlan(tasks=tasks)


async def test_unknown_source_is_dropped() -> None:
    payload = {
        "tasks": [
            {"source": "avito", "query": "скутер", "lang": "ru", "priority": 1},
            {"source": "chotot", "query": "xe ga", "lang": "vi", "priority": 1},
        ],
        "reasoning": "",
    }

    plan = await SearchPlanner(FakeBroker(payload)).plan(make_passport(), SOURCES)

    # Адаптера под выдуманный источник не существует, задача бесполезна.
    assert plan.sources() == ["chotot"]


@pytest.mark.parametrize(
    "payload",
    [
        {"tasks": "скутер, инжектор", "reasoning": "строка вместо списка"},
        {"tasks": [], "reasoning": "пустой план"},
        # Пустой запрос источнику, который ищет текстом, искать нечем. Доске
        # пустой `q` наоборот законен и лучше слова — см. test_market_vocabulary.
        {"tasks": [{"source": "telegram_groups", "query": "  "}], "reasoning": "пустой запрос"},
        {"reasoning": "задач нет вовсе"},
        ["не объект вообще"],
    ],
    ids=["not_a_list", "empty", "blank_query", "no_tasks_key", "not_an_object"],
)
async def test_garbage_answer_falls_back(payload: Any) -> None:
    plan = await SearchPlanner(FakeBroker(payload)).plan(make_passport(), SOURCES)

    assert plan.is_fallback
    assert plan.tasks, "фолбэк обязан дать план, иначе бот молчит"


@pytest.mark.parametrize(
    "error",
    [
        BrokerError("провайдер вернул невалидный JSON"),
        BrokerCapError("daily budget cap reached"),
        httpx.ConnectError("брокер недоступен"),
        TimeoutError("план не собрался за таймаут"),
    ],
    ids=["invalid_json", "cap_reached", "connect_error", "timeout"],
)
async def test_broker_failure_falls_back(error: Exception) -> None:
    plan = await SearchPlanner(FakeBroker(error=error)).plan(make_passport(), SOURCES)

    assert plan.is_fallback
    assert plan.tasks
    assert set(plan.sources()) <= set(SOURCES)


async def test_empty_registry_costs_nothing() -> None:
    broker = FakeBroker(GOOD_PAYLOAD)

    plan = await SearchPlanner(broker).plan(make_passport(), [])

    assert plan.tasks == []
    assert not broker.calls  # искать нечем — квоту модели не тратим


def test_fallback_speaks_russian_and_vietnamese() -> None:
    # `web` в списке потому, что вьетнамское СЛОВО теперь уезжает только
    # источникам со свободным текстом: у структурной доски `q` складывается с
    # фильтрами через И, и «tay ga» с `motorbiketype=3` даёт ноль вместо 12.
    plan = fallback_plan(make_passport(), [*SOURCES, "web"], reason="тест")
    queries = [task.query for task in plan.tasks]

    assert any("скутер" in query for query in queries)
    # «tay ga», не «xe ga»: замер по Нячангу — «tay ga» 41 объявление,
    # «xe ga» ровно ноль. Словарь чинён по живой выдаче, см. market_terms.
    assert any("tay ga" in query for query in queries)
    assert {"ru", "vi"} <= {task.lang for task in plan.tasks}
    assert len(plan.tasks) <= MAX_TASKS
    # Вьетнамский не идёт в Telegram: вьетнамцы там байки не продают.
    assert all(task.source != "telegram_groups" for task in plan.tasks if task.lang == "vi")
    assert all(task.params["city"] == "nha_trang" for task in plan.tasks)


def test_fallback_mirrors_intent() -> None:
    """Клиент снимает жильё — искать надо тех, кто сдаёт."""
    # Глагол сделки — приём прозы: доске он ломает даже рабочее слово (замер:
    # «nguyên zin» 59, «bán nguyên zin» 0), поэтому вьетнамский глагол теперь
    # виден у источника со свободным текстом, а не у Chotot.
    plan = fallback_plan(
        make_passport(intent=Intent.RENT, category=Category.APARTMENT),
        ["web"],
        reason="тест",
    )
    queries = " | ".join(task.query for task in plan.tasks)

    assert "сдам" in queries
    assert "cho thuê" in queries
    assert "продам" not in queries


def test_fallback_without_category_uses_client_words() -> None:
    plan = fallback_plan(
        make_passport(category=Category.OTHER, raw_query="ищу холодильник"),
        SOURCES,
        reason="тест",
    )

    assert plan.tasks
    assert any("холодильник" in task.query for task in plan.tasks)


def test_prompt_carries_market_vocabulary() -> None:
    prompt = build_user_prompt(make_passport(), [*SOURCES, "web"])

    assert "tay ga" in prompt  # вьетнамский словарь виден модели
    assert "инжектор" in prompt
    assert "Нячанг" in prompt
    assert "chotot: языки vi" in prompt
    assert "motorbike" in prompt
    assert "до 400 USD разово" in prompt


def test_system_prompt_demands_expansion() -> None:
    lowered = SYSTEM_PROMPT.lower()

    assert "не бери слова клиента буквально" in lowered
    assert "синонимы" in lowered
    assert "жаргон" in lowered
    assert "переводы" in lowered


def test_schema_enum_comes_from_registry() -> None:
    task_schema = plan_schema(SOURCES)["properties"]["tasks"]

    # Модель физически не может назвать источник, под который нет адаптера.
    assert task_schema["items"]["properties"]["source"]["enum"] == SOURCES
    assert task_schema["maxItems"] == MAX_TASKS
