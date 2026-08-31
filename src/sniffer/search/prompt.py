"""Промпт и строгая схема планировщика.

Отделено от планировщика: планировщик отвечает за вызов и за поведение при
отказе, промпт — за то, какими словами ищем. Правится это по разным причинам и
разными людьми.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sniffer.domain.passport import Budget, Passport, PricePeriod
from sniffer.search.plan import LOW_PRIORITY, MAX_TASKS, TOP_PRIORITY
from sniffer.search.vocabulary import (
    attribute_phrases,
    board_attribute_phrases,
    category_terms,
    city_name,
    jargon_terms,
    plan_langs,
    source_profile,
)

# Паспорт хранит период машинным значением; модели читать «за once» незачем.
_PERIOD_RU: dict[PricePeriod, str] = {
    PricePeriod.ONCE: "разово",
    PricePeriod.DAY: "в сутки",
    PricePeriod.WEEK: "в неделю",
    PricePeriod.MONTH: "в месяц",
}

SYSTEM_PROMPT = f"""Ты планировщик поиска частных объявлений. На входе паспорт
запроса клиента и список доступных источников. На выходе план: какой запрос, к
какому источнику и на каком языке отправить.

ГЛАВНОЕ ПРАВИЛО: не бери слова клиента буквально. Продавец пишет объявление не
теми словами, которыми покупатель формулирует запрос. Проверено вручную на
рынке байков в Нячанге: по слову «скутер» находится меньше половины лотов, а
«инжектор» и «блюкарт» вытаскивают то, чего «скутер» не видит.

Поэтому для каждого источника подбирай:
1. синонимы и разговорные названия предмета;
2. жаргон рынка — то, чем продавец хвастается и что покупатель проверяет
   (инжектор, блюкарт, cà vẹt, свежая резина);
3. переводы на язык, на котором продавцы этого источника реально пишут;
4. бренды и модели, ходовые для этой категории в этом городе.

ЯЗЫК ВЫБИРАЕТСЯ ПО ИСТОЧНИКУ, А НЕ ПО ЯЗЫКУ КЛИЕНТА. Вьетнамцы в Telegram
байки не продают вообще — их рынок на Chotot. Русский запрос к вьетнамской
доске вернёт ноль, вьетнамский в русском чате Нячанга — тоже ноль. Для каждого
источника ниже указано, на чём там пишут; если у источника несколько языков,
покрой запросами каждый.

ГОРОД в текст запроса добавляй только тем источникам, у которых ниже помечено
«город в запрос: да» — это те, что ищут по всему интернету. В чате конкретного
города название города в объявлениях почти не пишут, и в поисковой строке оно
только режет выдачу. Город и категория передаются исполнителю отдельно всегда.

ЖАРГОН — только источникам с пометкой «жаргон: да», то есть тем, где объявление
написано прозой. У структурной доски объявление собрано из полей, жаргона там
нет ни в одном: запрос «инжектор» вернул на такой доске ноль объявлений, и
задача из бюджета плана пропала впустую.

АТРИБУТЫ ПАСПОРТА В ТЕКСТ ЗАПРОСА НЕ ВПИСЫВАЙ. Коробка передач, объём двигателя,
бренд, год и бюджет уезжают исполнителю отдельно и превращаются в фильтры того
источника, у которого такие поля есть — это точнее любого слова. Замерено: общий
запрос по категории мотобайков в Нячанге даёт 71% настоящих скутеров-автоматов,
а тот же запрос со структурным фильтром типа кузова — 100%. Твоя работа — слова
предмета; отбор по свойствам делает исполнитель.

У ИСТОЧНИКА С ПОМЕТКОЙ «жаргон: нет» ОСТАВЛЯЙ query ПУСТЫМ. Такой источник ищет
полями, и текст там складывается с фильтрами через И: слово о свойстве не
уточняет фильтр, а гасит его в ноль. Замер: фильтр «механика» даёт 12
объявлений, он же со словом «côn tay» в тексте — ноль, и клиент видит пустоту,
которую прочтёт как «на рынке нет». Единственное исключение — конкретное имя
модели («Honda Vision», «Air Blade»): это настоящий текст объявления. Слова
свойств для таких источников перечислены ниже отдельным списком, и он короткий,
потому что в нём только измеренные; чего там нет — не пиши.

ОГРАНИЧЕНИЯ:
- не больше {MAX_TASKS} задач: каждая стоит запроса к источнику и времени;
- priority {TOP_PRIORITY} — почти наверняка даст попадания, 2 — вероятно,
  {LOW_PRIORITY} — догадка; догадок не больше трети плана;
- source бери только из списка доступных, других адаптеров не существует;
- params — параметры конкретного источника; не знаешь наверняка — оставь
  пустым, пустой список честнее выдуманного фильтра;
- reasoning — одно-два предложения по-русски: почему такие источники и языки."""


def build_user_prompt(passport: Passport, sources: Sequence[str]) -> str:
    blocks = [
        "Паспорт запроса:",
        _passport_block(passport),
        "",
        "Доступные источники и языки, на которых там пишут продавцы:",
        _sources_block(sources),
    ]
    vocabulary = _vocabulary_block(passport, sources)
    if vocabulary:
        blocks += ["", vocabulary]
    return "\n".join(blocks)


def plan_schema(sources: Sequence[str]) -> dict[str, Any]:
    """Строгая схема плана.

    Список источников подставляется в `enum` из реестра, поэтому модель
    физически не может назвать источник, под который нет адаптера, — это
    дешевле, чем ловить выдумку постфактум валидацией.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["tasks", "reasoning"],
        "properties": {
            "tasks": {
                "type": "array",
                "maxItems": MAX_TASKS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["source", "query", "lang", "params", "priority"],
                    "properties": {
                        "source": {"type": "string", "enum": list(sources)},
                        "query": {"type": "string"},
                        "lang": {"type": "string", "description": "код языка запроса, ru|en|vi"},
                        "priority": {
                            "type": "integer",
                            "minimum": TOP_PRIORITY,
                            "maximum": LOW_PRIORITY,
                        },
                        # strict json_schema не допускает объект со свободными
                        # ключами, поэтому параметры едут парами строк.
                        "params": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["key", "value"],
                                "properties": {
                                    "key": {"type": "string"},
                                    "value": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
            "reasoning": {"type": "string"},
        },
    }


def _passport_block(passport: Passport) -> str:
    city = passport.city or ""
    rows = [
        ("намерение клиента", passport.intent.value if passport.intent else ""),
        ("категория", passport.category.value if passport.category else ""),
        ("город", f"{city_name(city, 'ru')} ({city})" if city else ""),
        ("районы", ", ".join(passport.districts)),
        ("бюджет", _budget_line(passport.budget)),
        ("атрибуты", ", ".join(f"{key}={value}" for key, value in passport.attributes.items())),
        ("обязательно", ", ".join(passport.must_have)),
        ("недопустимо", ", ".join(passport.deal_breakers)),
        ("формулировка клиента", passport.raw_query),
    ]
    return "\n".join(f"- {label}: {value}" for label, value in rows if value)


def _budget_line(budget: Budget) -> str:
    if budget.min is None and budget.max is None:
        return ""
    currency = budget.currency.value if budget.currency else ""
    bounds = "–".join(f"{value:g}" for value in (budget.min, budget.max) if value is not None)
    prefix = "до " if budget.min is None else ""
    return f"{prefix}{bounds} {currency} {_PERIOD_RU[budget.period]}".strip()


def _sources_block(sources: Sequence[str]) -> str:
    """Профиль источника целиком: язык, жаргон, город. Всё из словаря рынка."""
    rows = []
    for source in sources:
        profile = source_profile(source)
        rows.append(
            f"- {source}: языки {', '.join(profile.langs)}"
            f"; жаргон: {_yes_no(profile.free_text)}"
            f"; город в запрос: {_yes_no(profile.city_in_query)}"
        )
    return "\n".join(rows)


def _yes_no(value: bool) -> str:
    return "да" if value else "нет"


def _vocabulary_block(passport: Passport, sources: Sequence[str]) -> str:
    """Известный словарь рынка — затравка, чтобы модель не начинала с нуля.

    Разбит по типу источника, а не выдан одним списком. Иначе промпт спорит сам
    с собой: правило выше запрещает писать свойства в текст запроса, а список
    показывает модели ровно эти слова — и она напишет. Слова для прозы и слова,
    измеренно безопасные для источника, ищущего полями, — разные наборы, и
    второй короткий: «côn tay» в тексте гасит верный фильтр в ноль.
    """
    prose = [source for source in sources if source_profile(source).free_text]
    board = [source for source in sources if not source_profile(source).free_text]

    blocks = [block for block in (_prose_block(passport, prose), _board_block(passport, board))]
    return "\n\n".join(block for block in blocks if block)


def _prose_block(passport: Passport, sources: Sequence[str]) -> str:
    if not sources:
        return ""
    langs = plan_langs(list(sources))
    lines = [
        line
        for lang in langs
        if (line := _lang_line(f"{lang}, предмет", category_terms(passport.category, lang)))
    ]
    lines += [
        line
        for lang in langs
        if (
            line := _lang_line(
                f"{lang}, свойства из паспорта",
                tuple(attribute_phrases(passport.category, passport.attributes, lang)),
            )
        )
    ]
    lines += [
        line
        for lang in langs
        if (line := _lang_line(f"{lang}, жаргон", jargon_terms(passport.category, lang)))
    ]
    if not lines:
        return ""
    return "\n".join(
        [
            "Словарь рынка для источников со свободным текстом "
            f"({', '.join(sources)}) — отправная точка, а не список:",
            *lines,
        ]
    )


def _board_block(passport: Passport, sources: Sequence[str]) -> str:
    """Что вообще можно писать в текст источнику, который ищет полями."""
    if not sources:
        return ""
    langs = plan_langs(list(sources))
    lines = [
        line
        for lang in langs
        if (
            line := _lang_line(
                f"{lang}, проверенные замером слова",
                tuple(board_attribute_phrases(passport.category, passport.attributes, lang)),
            )
        )
    ]
    header = (
        f"Источники, которые ищут полями ({', '.join(sources)}): свойства из паспорта уезжают "
        "туда фильтрами, писать их словами нельзя — слово о свойстве гасит фильтр в ноль."
    )
    if not lines:
        return f"{header} Проверенных замером слов для этого паспорта нет: оставь query пустым."
    return "\n".join([f"{header} Из слов допустимы только эти:", *lines])


def _lang_line(label: str, terms: tuple[str, ...]) -> str:
    return f"- {label}: {', '.join(terms)}" if terms else ""
