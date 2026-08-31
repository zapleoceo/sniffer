"""Словарь рынка: какими словами торгуют, а не какими формулируют запрос.

Знание одно, потребителей два: промпт отдаёт его модели как отправную точку,
фолбэк строит из него готовый план. Продублировать его в обоих местах — значит
однажды поправить только одно.

Здесь профили источников и функции доступа; сами слова — в `market_terms`.

Доступ двойной, и это не удобство, а защита: `attribute_phrases()` отдаёт слова
для прозы, `board_attribute_phrases()` — измеренное подмножество, которое можно
отправить в `q` источнику, ищущему полями. Одна функция на оба случая означала бы
жаргон в запросе к структурной доске, а это не «менее точная выдача», а пустая.

Профиль источника — это данные, а не ветвление. Ни планировщик, ни фолбэк не
знают слова «chotot»: они спрашивают у профиля «принимает ли этот источник
жаргон» и «нужен ли ему город в тексте». Новый источник — строка в
SOURCE_PROFILES; забыли строку — работает по осторожному DEFAULT_PROFILE.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sniffer.domain.passport import Category, Intent
from sniffer.search.market_terms import (
    ATTRIBUTE_TERMS,
    BOARD_ATTRIBUTE_TERMS,
    BOARD_QUERY_HITS,
    BOARD_QUERY_TOTAL,
    CATEGORY_TERMS,
    CITY_NAMES,
    INTENT_TERMS,
    JARGON,
    MARKET_LANGS,
    LangTerms,
)


@dataclass(frozen=True, slots=True)
class SourceProfile:
    """Чем этот источник отличается для составителя запроса.

    Три факта, и все три выведены из провалов, а не из вкуса:

    `langs` — на чём пишут продавцы ИМЕННО здесь. Русский запрос к вьетнамской
    доске возвращает ноль, вьетнамский в русском чате Нячанга — тоже ноль.

    `free_text` — свободный ли это текст. В чате объявление пишут прозой, и
    жаргон («инжектор», «блюкарт») вытаскивает лоты, которых не видно по
    названию предмета. Структурная доска жаргона не знает: замер по Chotot —
    «скутер» 0, «инжектор» 0, «блюкарт» 0 объявлений.

    Флаг решает судьбу ВСЕГО текста, а не только жаргона. У доски `q` живёт не
    вместо фильтров, а вместе с ними, через И: слово о свойстве гасит верный
    фильтр в ноль (`motorbiketype=3` — 12 объявлений, он же с `q='côn tay'` —
    ноль). Поэтому `free_text=False` означает «в `q` пускать только измеренное»,
    и обычно это пустой `q`: отбор делают поля.

    `city_in_query` — надо ли вклеивать город в текст. Источнику, который ищет
    по всему интернету, город необходим; чату конкретного города он только
    режет выдачу, потому что в объявлениях его не пишут.
    """

    langs: tuple[str, ...]
    free_text: bool
    city_in_query: bool


# Незнакомый источник получает все языки рынка, жаргон и город: без записи в
# профиле новый адаптер должен работать, пусть и вслепую. Осторожность здесь —
# в сторону полноты выдачи, а не точности: пустой результат хуже шумного.
DEFAULT_PROFILE = SourceProfile(langs=MARKET_LANGS, free_text=True, city_in_query=True)

SOURCE_PROFILES: dict[str, SourceProfile] = {
    "telegram_groups": SourceProfile(langs=("ru", "en"), free_text=True, city_in_query=False),
    "telegram_discover": SourceProfile(langs=("ru", "en"), free_text=True, city_in_query=False),
    # Chotot — структурная доска: тип, объём и бренд там отдельные поля, а `q`
    # ведёт себя непредсказуемо (замер: «tay ga» 41, «xe ga» 0, «xe máy» 59,
    # то есть столько же, сколько без запроса вообще). Отбор делают params.
    "chotot": SourceProfile(langs=("vi",), free_text=False, city_in_query=False),
    "web": SourceProfile(langs=("vi", "ru"), free_text=True, city_in_query=True),
    "facebook": SourceProfile(langs=("vi", "en"), free_text=True, city_in_query=True),
}


def source_profile(source: str) -> SourceProfile:
    return SOURCE_PROFILES.get(source, DEFAULT_PROFILE)


def source_langs(source: str) -> tuple[str, ...]:
    return source_profile(source).langs


def accepts_jargon(source: str) -> bool:
    return source_profile(source).free_text


def wants_city_in_query(source: str) -> bool:
    return source_profile(source).city_in_query


def plan_langs(sources: list[str]) -> list[str]:
    """Языки, которые вообще имеют смысл для этого набора источников."""
    langs = [lang for source in sources for lang in source_langs(source)]
    return list(dict.fromkeys(langs))


def category_terms(category: Category | None, lang: str) -> tuple[str, ...]:
    if category is None:
        return ()
    return CATEGORY_TERMS.get(category, {}).get(lang, ())


def intent_terms(intent: Intent | None, lang: str) -> tuple[str, ...]:
    if intent is None:
        return ()
    return INTENT_TERMS.get(intent, {}).get(lang, ())


def jargon_terms(category: Category | None, lang: str) -> tuple[str, ...]:
    if category is None:
        return ()
    return JARGON.get(category, {}).get(lang, ())


def attribute_terms(
    category: Category | None, attribute: str, value: object, lang: str
) -> tuple[str, ...]:
    """Как значение атрибута звучит на языке рынка — для ПРОЗЫ.

    Булев атрибут в паспорте лежит как `True`, а в таблице ключ — строка
    `"true"`: словарь остаётся данными и не зависит от типов паспорта.

    Источнику, который ищет полями, эти слова отправлять нельзя — для него есть
    `board_attribute_phrases()`.
    """
    return _terms(ATTRIBUTE_TERMS, category, attribute, value, lang)


def attribute_phrases(
    category: Category | None, attributes: dict[str, object], lang: str
) -> list[str]:
    """Слова всех заполненных атрибутов паспорта — по одному разу и по порядку."""
    return _phrases(attribute_terms, category, attributes, lang)


def board_attribute_terms(
    category: Category | None, attribute: str, value: object, lang: str
) -> tuple[str, ...]:
    """То же, но только измеренное подмножество, безопасное как `q` у доски.

    Структурная доска складывает `q` со своими фильтрами через И, поэтому слово
    о свойстве, которое доска отбирает полем, гасит верный фильтр в ноль:
    `motorbiketype=3` даёт 12 объявлений, а он же с `q='côn tay'` — ноль. Пустой
    ответ здесь — норма и правильный результат замера, а не пробел в словаре.
    """
    return _terms(BOARD_ATTRIBUTE_TERMS, category, attribute, value, lang)


def board_attribute_phrases(
    category: Category | None, attributes: dict[str, object], lang: str
) -> list[str]:
    """Слова атрибутов, которые доске отправить измеренно безопасно."""
    return _phrases(board_attribute_terms, category, attributes, lang)


def board_query_hits(term: str) -> int | None:
    """Сколько объявлений слово отдало как `q` структурной доске. None — не мерили."""
    return BOARD_QUERY_HITS.get(term.strip().casefold())


def is_board_safe(term: str) -> bool:
    """Годится ли слово в `q` доски: измерено, не ноль и не вся выдача.

    Неизмеренное слово безопасным не считается — это и есть гейт против
    «казалось бы, подходит»: цена ошибки не «выдача чуть уже», а пустота.
    """
    hits = board_query_hits(term)
    return hits is not None and 0 < hits < BOARD_QUERY_TOTAL


def _terms(
    table: dict[Category, dict[str, dict[str, LangTerms]]],
    category: Category | None,
    attribute: str,
    value: object,
    lang: str,
) -> tuple[str, ...]:
    if category is None or value is None:
        return ()
    key = str(value).strip().lower()
    if not key:
        return ()
    return table.get(category, {}).get(attribute, {}).get(key, {}).get(lang, ())


def _phrases(
    terms: Callable[[Category | None, str, object, str], tuple[str, ...]],
    category: Category | None,
    attributes: dict[str, object],
    lang: str,
) -> list[str]:
    phrases: list[str] = []
    for attribute, value in attributes.items():
        phrases += terms(category, attribute, value, lang)
    return list(dict.fromkeys(phrases))


def city_name(city: str | None, lang: str) -> str:
    """Незнакомый город разворачиваем из слага: `da_lat` → `Da Lat`."""
    if not city:
        return ""
    known = CITY_NAMES.get(city)
    if known:
        return known.get(lang, known.get("en", city))
    return city.replace("_", " ").title()
