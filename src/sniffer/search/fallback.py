"""Шаблонный план на случай, когда модель недоступна.

Без него недоступность брокера означает мёртвого бота: паспорт есть, источники
есть, а искать нечего. Дневной cap проекта в AIbroker исчерпывается терминально
(BrokerCapError не ретраится до полуночи UTC), то есть это не гипотетическая
авария, а штатное состояние конца суток.

План заведомо грубее LLM-плана: он не угадывает модели байков под конкретную
формулировку. Но словарь рынка, жаргон, выбор языка по источнику и структурные
фильтры из атрибутов у него ТЕ ЖЕ САМЫЕ — иначе фолбэк тихо разъедется с
боевым путём и сломается ровно тогда, когда он единственный работает
(spec-v2, 2.4). Атрибуты доезжают до источника через `context_params`, а не
через текст запроса, поэтому `transmission=automatic` становится фильтром и
здесь, без участия модели.
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
from sniffer.search.vocabulary import (
    attribute_phrases,
    category_terms,
    city_name,
    intent_terms,
    jargon_terms,
    source_langs,
    source_profile,
)

# Потолок задач общий (12), источников может быть много: берём по два предмета
# на язык, чтобы ни один источник не съел весь план целиком. Общий отбор идёт
# по приоритету, поэтому «главный» запрос каждого источника переживает обрезку.
TERMS_PER_LANG = 2
JARGON_PER_LANG = 2


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
    profile = source_profile(source)
    # Город в текст — только источникам, которые ищут по всему интернету. В чате
    # Нячанга слово «Нячанг» в объявлениях не пишут, и оно лишь режет выдачу.
    city = city_name(passport.city, lang) if profile.city_in_query else ""
    verbs = intent_terms(passport.intent, lang)

    tasks: list[SearchTask] = []
    for index, noun in enumerate(_nouns(passport, source, lang)):
        priority = TOP_PRIORITY if index == 0 else DEFAULT_PRIORITY
        tasks.append(_task(source, _with_city(noun, city), lang, priority))
        if verbs:
            # Пара «глагол сделки + предмет» вытаскивает объявления, где предмет
            # назван непривычно, а глагол стандартный: «продам педальник».
            tasks.append(
                _task(
                    source,
                    _with_city(f"{verbs[0]} {noun}", city),
                    lang,
                    min(priority + 1, LOW_PRIORITY),
                )
            )

    if profile.free_text:
        # Жаргон — отдельные запросы, а не приставка к предмету: смысл его в том,
        # что предмет продавец вообще не назвал. Полевые заметки (spec-v2, 7):
        # «инжектор» и «блюкарт» находили лоты, которых не находил «скутер».
        # Структурной доске жаргон не отправляем — замер по Chotot дал ноль.
        for term in jargon_terms(passport.category, lang)[:JARGON_PER_LANG]:
            tasks.append(_task(source, _with_city(term, city), lang, DEFAULT_PRIORITY))
    return tasks


def _nouns(passport: Passport, source: str, lang: str) -> list[str]:
    """Чем назвать предмет. Порядок = порядок убывания уверенности.

    Различие по типу источника, а не по его имени: у структурной доски свойство
    предмета это отдельное поле, и её собственное слово («tay ga») отбирает
    лучше любого описания. В чате полей нет, поэтому свойство приклеивается к
    предмету словом («скутер автомат»), иначе «автомат» приведёт стиральные
    машины.
    """
    attributes = attribute_phrases(passport.category, passport.attributes, lang)
    generic = list(category_terms(passport.category, lang))

    if source_profile(source).free_text:
        nouns = list(_compound(generic, attributes))
        nouns += generic
    else:
        # Слово рынка вперёд, общие названия после: список категории намеренно
        # содержит и «xe số» (лапка), а он клиенту с автоматом не нужен.
        nouns = attributes + [term for term in generic if term not in attributes]

    brand = str(passport.attributes.get("brand", "")).strip()
    if brand:
        # Бренд пишется одинаково на всех трёх языках рынка, поэтому он идёт
        # первым запросом в любом из них.
        nouns.insert(0, brand)
    if not nouns and passport.raw_query.strip():
        # Категорию не распознали — ищем словами клиента. Для вьетнамского
        # источника это заведомо слабо, но пустой план хуже слабого.
        nouns = [passport.raw_query.strip()]
    return list(dict.fromkeys(nouns))[:TERMS_PER_LANG]


def _compound(generic: list[str], attributes: list[str]) -> tuple[str, ...]:
    """«скутер автомат» — предмет плюс то, чем он должен быть.

    Пусто, когда склеивать нечего или когда одно слово уже содержит другое: по-
    вьетнамски скутер и есть «tay ga», и «tay ga tay ga» — не запрос, а опечатка.
    """
    if not generic or not attributes:
        return ()
    noun, quality = generic[0], attributes[0]
    if noun.casefold() in quality.casefold() or quality.casefold() in noun.casefold():
        return ()
    return (f"{noun} {quality}",)


def _task(source: str, query: str, lang: str, priority: int) -> SearchTask:
    return SearchTask(source=source, query=query, lang=lang, priority=priority)


def _with_city(query: str, city: str) -> str:
    return f"{query} {city}".strip() if city else query


def _reasoning(passport: Passport, sources: list[str], reason: str) -> str:
    category = passport.category.value if passport.category else "категория неизвестна"
    city = city_name(passport.city, "ru") or "город неизвестен"
    return (
        f"шаблонный план без модели ({reason}): {category}, {city}; "
        f"источники {', '.join(sources)}, язык запросов выбран по источнику"
    )
