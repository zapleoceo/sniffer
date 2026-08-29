"""План поиска: что спрашивать, у какого источника и на каком языке.

Модель плана отделена от планировщика намеренно. План рождается в двух местах —
из ответа LLM и из детерминированного фолбэка, — и оба обязаны пройти одну и ту
же нормализацию. Иначе фолбэк тихо разъезжается с боевым путём и ломается ровно
в тот момент, когда он единственный работает.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from sniffer.domain.passport import Passport

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
    def _query_filled(cls, value: str) -> str:
        stripped = " ".join(value.split())
        if not stripped:
            raise ValueError("пустой запрос")
        # Модель иногда выдаёт вместо запроса пересказ объявления. Источники на
        # такое отвечают нулём совпадений, поэтому режем по длине.
        return stripped[:MAX_QUERY_LEN]

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
    """
    params: dict[str, Any] = {}
    if passport.city:
        params["city"] = passport.city
    if passport.category:
        params["category"] = passport.category.value
    return params


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
        try:
            tasks.append(
                SearchTask(
                    source=source,
                    query=str(item.get("query", "")),
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
