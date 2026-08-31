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

И ровно поэтому текст запроса для источника, который ищет полями, собирается из
ОТДЕЛЬНОГО измеренного словаря. Фильтр и `q` складываются через И, так что
слово о том же свойстве не уточняет фильтр, а гасит его в ноль: замер —
`motorbiketype=3` даёт 12 объявлений, он же с `q='côn tay'` — ноль. Клиенту это
видно как «на рынке нет механики», хотя её двенадцать.
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
    board_attribute_phrases,
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
    # Глагол сделки приставкой к предмету — приём прозы. У доски он ломает даже
    # рабочее слово: замер — «nguyên zin» 59, «bán nguyên zin» 0; «xe mới» 8,
    # «bán xe mới» 0. Поэтому глагол уходит только источникам со свободным текстом.
    verbs = intent_terms(passport.intent, lang) if profile.free_text else ()

    nouns = _nouns(passport, source, lang)
    if not nouns:
        # Доске текст не нужен: отбор делают её поля, а измеренно безопасного
        # слова для этого паспорта нет. Пустой `q` — это честные 12 объявлений
        # по `motorbiketype=3` вместо нуля по `q='côn tay'` (spec-v2 4.1.1).
        # Источнику, который ищет только текстом, пустой запрос бесполезен:
        # искать нечем, и задача из бюджета плана уйдёт впустую.
        return [] if profile.free_text else [_task(source, "", lang, TOP_PRIORITY)]

    tasks: list[SearchTask] = []
    for index, noun in enumerate(nouns):
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

    Различие по типу источника, а не по его имени — по флагу `free_text`, то
    есть по данным профиля.
    """
    if source_profile(source).free_text:
        return _prose_nouns(passport, lang)
    return _board_nouns(passport, lang)


def _prose_nouns(passport: Passport, lang: str) -> list[str]:
    """Слова для источника, где объявление написано прозой.

    Полей у чата нет, поэтому свойство приклеивается к предмету словом
    («скутер автомат»), иначе «автомат» приведёт стиральные машины.
    """
    generic = list(category_terms(passport.category, lang))
    qualities = attribute_phrases(passport.category, passport.attributes, lang)
    nouns = list(_compound(generic, qualities))
    nouns += generic

    brand = str(passport.attributes.get("brand", "")).strip()
    if brand:
        # Бренд пишется одинаково на всех трёх языках рынка, поэтому он идёт
        # первым запросом в любом из них.
        nouns.insert(0, brand)
    if not nouns and passport.raw_query.strip():
        # Категорию не распознали — ищем словами клиента. Слабо, но пустой план
        # хуже слабого: чат хотя бы поищет по тексту.
        nouns = [passport.raw_query.strip()]
    return list(dict.fromkeys(nouns))[:TERMS_PER_LANG]


def _board_nouns(passport: Passport, lang: str) -> list[str]:
    """Слова для источника, который ищет полями. Обычно ни одного — и это верно.

    Доска складывает `q` со своими структурными фильтрами через И, поэтому цена
    лишнего слова — не «выдача чуть уже», а пустота, которую клиент прочтёт как
    «на рынке нет». Замер 31.08.2026 (Нячанг, cg=2020, всего 59):

    - `motorbiketype=3` — 12 объявлений; он же с `q='côn tay'`, `q='xe côn tay'`
      или `q='tay ga'` — **ноль** в каждом случае;
    - `motorbiketype=2` — 5; с `q='bán tự động'` — ноль;
    - «cà vẹt», «cavet», «giấy tờ đầy đủ» — ноль каждое при 59 без запроса;
    - «chính chủ», «nguyên zin», «xe máy» — все 59, то есть не фильтруют ничего.

    Поэтому здесь только измеренное подмножество (`BOARD_ATTRIBUTE_TERMS`), а
    оно ПУСТО: последним туда добиралось «xe mới», и замер 31.08.2026 показал,
    что его 8 из 59 — это число слова «xe» (тот же набор id), все восемь
    объявлений помечены б/у, а состояние доска отбирает полем `condition_ad`.
    То есть слов о свойстве, безопасных для доски, нет ни одного.
    Названия предмета из `CATEGORY_TERMS` не годятся ни одно: «tay ga» и «xe số»
    — это `motorbiketype=1` и `=2` словами (пересечение полное), и с чужим
    значением фильтра они дают ноль; «xe máy» не фильтрует; «xe ga» не находит
    НИЧЕГО. Категорию доска и так знает полем `cg`.

    Бренд тоже не текстом: замер — `q='honda'` и `motorbikebrand=1` дают одни и
    те же 26, а поле не зависит от написания, которое прислал клиент. Незнакомый
    доске бренд остаётся без фильтра — честная полная выдача плюс ранжирование
    лучше неизмеренного слова, которое может вернуть пустоту.
    """
    return board_attribute_phrases(passport.category, passport.attributes, lang)[:TERMS_PER_LANG]


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
