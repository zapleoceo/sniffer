"""Шаблонный план на случай, когда модель недоступна.

Без него недоступность брокера означает мёртвого бота: паспорт есть, источники
есть, а искать нечего. Дневной cap проекта в AIbroker исчерпывается терминально
(BrokerCapError не ретраится до полуночи UTC), то есть это не гипотетическая
авария, а штатное состояние конца суток.

План заведомо грубее LLM-плана: он не угадывает модели и не подбирает жаргон
под конкретную формулировку — он берёт словарь рынка по категории и намерению.
Но он ищет, и это лучше, чем не искать.
"""

from __future__ import annotations

from sniffer.domain.passport import Passport
from sniffer.search.plan import (
    DEFAULT_PRIORITY,
    LOW_PRIORITY,
    TOP_PRIORITY,
    SearchPlan,
    SearchTask,
    context_params,
)
from sniffer.search.vocabulary import category_terms, city_name, intent_terms, source_langs

# Потолок задач общий (12), источников может быть много: берём по два предмета
# на язык, чтобы ни один источник не съел весь план целиком.
TERMS_PER_LANG = 2


def fallback_plan(passport: Passport, sources: list[str], *, reason: str) -> SearchPlan:
    tasks: list[SearchTask] = []
    for source in sources:
        for lang in source_langs(source):
            tasks.extend(_source_tasks(passport, source, lang))

    return SearchPlan.from_tasks(
        tasks,
        reasoning=_reasoning(passport, sources, reason),
        defaults=context_params(passport),
        is_fallback=True,
    )


def _source_tasks(passport: Passport, source: str, lang: str) -> list[SearchTask]:
    verbs = intent_terms(passport.intent, lang)
    tasks: list[SearchTask] = []
    for index, noun in enumerate(_nouns(passport, lang)):
        priority = TOP_PRIORITY if index == 0 else DEFAULT_PRIORITY
        tasks.append(SearchTask(source=source, query=noun, lang=lang, priority=priority))
        if verbs:
            # Пара «глагол сделки + предмет» вытаскивает объявления, где предмет
            # назван непривычно, а глагол стандартный: «продам педальник».
            tasks.append(
                SearchTask(
                    source=source,
                    query=f"{verbs[0]} {noun}",
                    lang=lang,
                    priority=min(priority + 1, LOW_PRIORITY),
                )
            )
    return tasks


def _nouns(passport: Passport, lang: str) -> list[str]:
    nouns = list(category_terms(passport.category, lang))
    brand = str(passport.attributes.get("brand", "")).strip()
    if brand:
        # Бренд пишется одинаково на всех трёх языках рынка, поэтому он идёт
        # первым запросом в любом из них.
        nouns.insert(0, brand)
    if not nouns and passport.raw_query.strip():
        # Категорию не распознали — ищем словами клиента. Для вьетнамского
        # источника это заведомо слабо, но пустой план хуже слабого.
        nouns = [passport.raw_query.strip()]
    return nouns[:TERMS_PER_LANG]


def _reasoning(passport: Passport, sources: list[str], reason: str) -> str:
    category = passport.category.value if passport.category else "категория неизвестна"
    city = city_name(passport.city, "ru") or "город неизвестен"
    return (
        f"шаблонный план без модели ({reason}): {category}, {city}; "
        f"источники {', '.join(sources)}, язык запросов выбран по источнику"
    )
