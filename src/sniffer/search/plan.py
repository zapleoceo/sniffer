"""План поиска: что спрашивать, у какого источника и на каком языке.

Модель плана отделена от планировщика намеренно. План рождается в двух местах —
из ответа LLM и из детерминированного фолбэка, — и оба обязаны пройти одну и ту
же нормализацию. Иначе фолбэк тихо разъезжается с боевым путём и ломается ровно
в тот момент, когда он единственный работает.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any

import structlog
from pydantic import BaseModel, Field, ValidationError, field_validator

from sniffer.domain.passport import Passport
from sniffer.search.vocabulary import board_query_allowed, source_profile

log = structlog.get_logger(__name__)

# spec-v2, раздел 2.3: свобода источников не означает свободу расходов.
MAX_TASKS = 12
MAX_QUERY_LEN = 120
DEFAULT_LANG = "ru"
TOP_PRIORITY = 1
DEFAULT_PRIORITY = 2
LOW_PRIORITY = 3


class SearchTask(BaseModel):
    """Один запрос к одному источнику."""

    source: str
    query: str
    lang: str = DEFAULT_LANG
    # Параметры конкретного источника плюс контекст паспорта: адаптер получает
    # только (query, params) и паспорта не видит.
    params: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=DEFAULT_PRIORITY, ge=TOP_PRIORITY, le=LOW_PRIORITY)

    @field_validator("source")
    @classmethod
    def _source_filled(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("источник не указан")
        return stripped

    @field_validator("query")
    @classmethod
    def _normalise_query(cls, value: str) -> str:
        # Пустой запрос — законная задача для источника, который ищет полями:
        # `q` складывается там с фильтрами через И, и слово о свойстве гасит
        # верный фильтр в ноль (spec-v2 4.1.1). Осмысленность пустоты проверяет
        # `parse_tasks` — она знает источник, а модель `SearchTask` не знает.
        #
        # Модель иногда выдаёт вместо запроса пересказ объявления. Источники на
        # такое отвечают нулём совпадений, поэтому режем по длине.
        return " ".join(value.split())[:MAX_QUERY_LEN]

    @field_validator("lang")
    @classmethod
    def _normalise_lang(cls, value: str) -> str:
        # Модель пишет то "vi", то "vietnamese", то "vi-VN". Ронять годный
        # запрос из-за формата кода языка дороже, чем привести его к двум буквам.
        code = value.strip().lower()[:2]
        return code if len(code) == 2 and code.isascii() and code.isalpha() else DEFAULT_LANG

    def dedup_key(self) -> tuple[str, str, str]:
        return self.source, self.query.casefold(), self.lang

    def with_defaults(self, defaults: Mapping[str, Any]) -> SearchTask:
        """Свои параметры источника всегда важнее подставленных из паспорта."""
        return self.model_copy(update={"params": {**defaults, **self.params}})


class SearchPlan(BaseModel):
    tasks: list[SearchTask] = Field(default_factory=list, max_length=MAX_TASKS)
    reasoning: str = ""
    # Отдельным полем, а не пометкой внутри reasoning: доля фолбэков — это
    # операционная метрика, по ней видно, что брокер лежит.
    is_fallback: bool = False

    @classmethod
    def from_tasks(
        cls,
        tasks: Sequence[SearchTask],
        reasoning: str = "",
        *,
        defaults: Mapping[str, Any] | None = None,
        is_fallback: bool = False,
    ) -> SearchPlan:
        """Единственный путь нормализации: дедуп, приоритет, потолок задач.

        Обрезаем по приоритету, а не по порядку перечисления: модель мешает
        очевидное с догадками, и потерять «скутер» ради «Honda Lead 2019 синий»
        нельзя.
        """
        unique: dict[tuple[str, str, str], SearchTask] = {}
        for task in tasks:
            unique.setdefault(task.dedup_key(), task)

        ordered = sorted(unique.values(), key=lambda task: task.priority)[:MAX_TASKS]
        if defaults:
            ordered = [task.with_defaults(defaults) for task in ordered]
        return cls(tasks=ordered, reasoning=reasoning.strip(), is_fallback=is_fallback)

    def sources(self) -> list[str]:
        return list(dict.fromkeys(task.source for task in self.tasks))


def context_params(passport: Passport) -> dict[str, Any]:
    """Что адаптер обязан получить помимо строки запроса.

    Город намеренно не вклеивается в текст запроса: в чате Нячанга слово
    «Нячанг» в объявлениях не пишут, и в поисковой строке оно только режет
    выдачу. Адаптеру, которому город нужен, он приезжает параметром.

    Атрибуты и бюджет едут в НЕЙТРАЛЬНОМ виде — как их сформулировал паспорт, а
    не как их называет источник. Перевод в чужие имена полей делает адаптер:
    `transmission=automatic` становится `motorbiketype=1` внутри Chotot, а
    планировщик про такое поле не знает и знать не должен. Иначе каждый новый
    источник правил бы планировщик, а это ровно то, что здесь запрещено.

    Едет всё, что заполнено, а не только то, что кто-то умеет отбирать: адаптер
    берёт понятное и молча игнорирует остальное.
    """
    params: dict[str, Any] = {}
    if passport.city:
        params["city"] = passport.city
    if passport.category:
        params["category"] = passport.category.value
    if passport.intent:
        params["intent"] = passport.intent.value
    attributes = {
        key: value for key, value in passport.attributes.items() if value not in (None, "", [], {})
    }
    if attributes:
        params["attributes"] = attributes
    budget = _budget_params(passport)
    if budget:
        params["budget"] = budget
    return params


def _budget_params(passport: Passport) -> dict[str, Any]:
    """Бюджет без валюты бесполезен: «до 400» это и доллары, и донги."""
    budget = passport.budget
    if budget.currency is None or (budget.min is None and budget.max is None):
        return {}
    return {
        "min": budget.min,
        "max": budget.max,
        "currency": budget.currency.value,
        "period": budget.period.value,
    }


def parse_tasks(raw: Any, available: Collection[str]) -> list[SearchTask]:
    """Разбор задач из ответа модели.

    Ответ модели — недоверенный ввод: битая задача выбрасывается, а не роняет
    весь план. Проверка `source` живёт здесь, а не в модели `SearchTask`:
    реестр источников лежит в БД и меняется без релиза, так что «известный
    источник» — это не свойство типа, а свойство момента.
    """
    if not isinstance(raw, list):
        return []

    known = set(available)
    tasks: list[SearchTask] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        source = str(item.get("source", "")).strip()
        if source not in known:
            continue
        profile = source_profile(source)
        query = str(item.get("query", ""))
        if not query.strip() and profile.free_text:
            # Источнику, который ищет только текстом, пустой запрос искать
            # нечем — задача уйдёт впустую. Источнику, который ищет полями,
            # пустой `q` наоборот лучший запрос: фильтры отбирают, а лишнее
            # слово гасит их в ноль (spec-v2 4.1.1).
            continue
        if query.strip() and not profile.free_text and not board_query_allowed(query):
            # Источник ищет полями, а модель прислала слово. Слово о свойстве
            # складывается с фильтром через И и гасит его в НОЛЬ: замер —
            # `motorbiketype=3` даёт 12 объявлений, он же с `q='côn tay'` ноль.
            # Выбрасываем текст, а не задачу: фильтры остаются и отбирают, а
            # клиент получает 12 объявлений вместо пустоты, прочитанной как «на
            # рынке нет». Разрешены только строки с замером (BOARD_SAFE_QUERIES),
            # и сейчас там пусто — ни одно слово замер не прошло.
            log.warning("plan.board_query_dropped", source=source, query=query.strip())
            query = ""
        try:
            tasks.append(
                SearchTask(
                    source=source,
                    query=query,
                    lang=str(item.get("lang", DEFAULT_LANG)),
                    params=_as_params(item.get("params")),
                    priority=_as_priority(item.get("priority")),
                )
            )
        except ValidationError:
            continue
    return tasks


def _as_params(raw: Any) -> dict[str, Any]:
    """Параметры приезжают парами строк — свободный объект strict-схема не берёт."""
    if isinstance(raw, Mapping):
        return {str(key): _as_scalar(value) for key, value in raw.items()}
    if isinstance(raw, list):
        return {
            str(item["key"]): _as_scalar(item["value"])
            for item in raw
            if isinstance(item, Mapping) and "key" in item and "value" in item
        }
    return {}


def _as_scalar(value: Any) -> Any:
    # Chotot ждёт cg=2020 числом, а из схемы значение приходит строкой.
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return value


def _as_priority(value: Any) -> int:
    try:
        priority = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PRIORITY
    return min(max(priority, TOP_PRIORITY), LOW_PRIORITY)
