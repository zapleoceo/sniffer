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

Здесь же чтение модельного ряда (`motorbike_models`), и здесь по той же
причине: имя модели ищут в тексте двое — разбор запроса клиента и отбор находок
перед показом, — а знание у них одно. Разложи его по обоим модулям, и однажды
поправят только один.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from sniffer.domain.passport import Category, Intent
from sniffer.search.market_terms import (
    ALL_CITY_NAMES,
    ATTRIBUTE_TERMS,
    BOARD_ATTRIBUTE_TERMS,
    BOARD_QUERY_HITS,
    BOARD_QUERY_TOTAL,
    BOARD_SAFE_QUERIES,
    CATEGORY_TERMS,
    CITY_ALIASES,
    CITY_NAMES,
    INTENT_TERMS,
    JARGON,
    MARKET_LANGS,
    LangTerms,
)
from sniffer.search.motorbike_models import (
    MODELS_BY_CATEGORY,
    MOTORBIKE_BRANDS,
    TRANSMISSION_BY_BODY,
    MotorbikeModel,
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
    # Собственный архив уже нормализован полями; текст нужен ранжированию
    # после выборки, а не SQL-фильтру до неё.
    "archive": SourceProfile(langs=("ru",), free_text=False, city_in_query=False),
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
    """Годится ли слово в `q` доски: измерено, число в границах и число СВОЁ.

    Неизмеренное слово безопасным не считается — это и есть гейт против
    «казалось бы, подходит»: цена ошибки не «выдача чуть уже», а пустота.

    Третье условие добавлено по разбору «xe mới»: 8 из 59 — не ноль и не вся
    выдача, прежним двум условиям слово удовлетворяло, а фильтром не было. Его
    находки совпали с `q='xe'` до последнего id, «mới» отдельно отдавало все 59,
    и все восемь были помечены б/у при запросе НОВОГО. Проверять «значит ли
    слово то, что написано» по одному числу нельзя, зато видно механически:
    фраза, чьё число равно числу её же измеренной части, ничего не добавляет к
    этой части. Это и проверяется — данными той же таблицы, а не списком
    исключений, который пришлось бы дописывать после каждого следующего случая.
    """
    hits = board_query_hits(term)
    if hits is None or not 0 < hits < BOARD_QUERY_TOTAL:
        return False
    return borrowed_from(term) is None


def borrowed_from(term: str) -> str | None:
    """Измеренная часть фразы, чьё число фраза повторяет. `None` — число своё.

    Возвращается сама часть, а не флаг: в логе и в тесте нужно видеть, У ЧЕГО
    слово заняло число, иначе разбираться придётся заново.
    """
    hits = board_query_hits(term)
    if hits is None:
        return None
    for part in _measured_parts(term):
        if BOARD_QUERY_HITS[part] == hits:
            return part
    return None


def board_query_allowed(query: str) -> bool:
    """Можно ли отправить эту строку в `q` источнику, который ищет полями.

    Список закрытый (`BOARD_SAFE_QUERIES`) и сейчас пустой: доска отбирает
    свойства полями, а слово о свойстве с чужим значением поля даёт ноль. Гейт
    нужен потому, что `q` приходит не только из нашего словаря — его присылает
    модель, и её текст к доске без замера пускать нельзя.
    """
    return query.strip().casefold() in {
        allowed.strip().casefold() for allowed in BOARD_SAFE_QUERIES
    }


def _measured_parts(term: str) -> list[str]:
    """Собственные подфразы термина, у которых есть свой замер."""
    words = term.strip().casefold().split()
    whole = " ".join(words)
    parts = []
    for start in range(len(words)):
        for end in range(start + 1, len(words) + 1):
            part = " ".join(words[start:end])
            if part != whole and part in BOARD_QUERY_HITS:
                parts.append(part)
    return parts


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
    """Незнакомый город разворачиваем из слага: `da_lat` → `Da Lat`.

    Берётся полный справочник, а не только обслуживаемые города: назвать Хойан
    Хойаном надо и в отказе «пока работаю только по Нячангу».
    """
    if not city:
        return ""
    known = ALL_CITY_NAMES.get(city)
    if known:
        return known.get(lang, known.get("en", city))
    return city.replace("_", " ").title()


def city_variants(slug: str) -> tuple[str, ...]:
    """Все написания города — этим ищут название в тексте клиента.

    Слаг тоже вариант: «nha trang» клиент пишет и латиницей.
    """
    names = ALL_CITY_NAMES.get(slug, {})
    return tuple(sorted({*names.values(), *CITY_ALIASES.get(slug, ()), slug.replace("_", " ")}))


def is_served(city: str | None) -> bool:
    """Ищем ли мы в этом городе.

    Пустой город — да: его подставит `default_city`, отказывать не за что.
    """
    return not city or city in CITY_NAMES


def served_cities(lang: str) -> tuple[str, ...]:
    """Названия обслуживаемых городов — для ответа «пока работаю только по …».

    Из того же словаря, что и поиск: список городов в тексте бота, набранный
    руками, разъехался бы с реальностью на первом же новом городе.
    """
    return tuple(city_name(slug, lang) for slug in CITY_NAMES)


ModelPatterns = tuple[tuple[str, re.Pattern[str]], ...]


def _patterns(models: tuple[MotorbikeModel, ...]) -> ModelPatterns:
    """Написания моделей → regex. Пробел значит «может быть, а может не быть».

    «air blade» и «airblade» — одно имя, и держать оба строками значило бы
    однажды забыть третье. Тот же приём, что у написаний города.

    У спортивных семейств recognition задано сырым `pattern`, а не написаниями:
    их имя слипается с объёмом («cbr150r», «z300»), и хвостовой `\\b` целого слова
    его бы не поймал (motorbike_models: поле `pattern`). Тогда написания не
    участвуют — regex берётся как есть.
    """
    return tuple((model.slug, re.compile(_model_regex(model), re.IGNORECASE)) for model in models)


def _model_regex(model: MotorbikeModel) -> str:
    if model.pattern is not None:
        return model.pattern
    return (
        r"\b(?:"
        + "|".join(re.escape(name).replace(r"\ ", r"\s*") for name in model.spellings)
        + r")\b"
    )


_MODELS: dict[str, MotorbikeModel] = {
    model.slug: model for models in MODELS_BY_CATEGORY.values() for model in models
}
# Категория модели — обратный ход по ТОЙ ЖЕ таблице, а не вторая рядом. Модельный
# ряд принадлежит категории (`MODELS_BY_CATEGORY`), значит ответ на «чей это
# предмет» в ней уже есть. Заведи вторую таблицу — она однажды разъедется с
# первой, и строка «Vision — это жильё» будет выглядеть данными, а не опечаткой.
_CATEGORY_BY_MODEL: dict[str, Category] = {
    model.slug: category for category, models in MODELS_BY_CATEGORY.items() for model in models
}
_PATTERNS_BY_CATEGORY: dict[Category, ModelPatterns] = {
    category: _patterns(models) for category, models in MODELS_BY_CATEGORY.items()
}
_ALL_PATTERNS: ModelPatterns = tuple(
    entry for patterns in _PATTERNS_BY_CATEGORY.values() for entry in patterns
)


def models_named_in(category: Category | None, text: str) -> tuple[str, ...]:
    """Модели, названные в тексте, — от самого конкретного написания к общему.

    Порядок задаёт ДЛИНА совпавшего написания, а не порядок строк в таблице:
    «winner x» конкретнее «winner», и решать, что назвал клиент, обязано
    написание. Ровно этим прежний разбор и болел — он брал первое совпадение
    regex, поэтому «honda lead» читалось как «honda».

    Одинаковую длину разводит позиция в тексте, а её — слаг: у одного и того же
    текста ответ обязан быть один и тот же всегда.

    Категория решает, какие имена вообще искать, и делает это таблицей, а не
    проверкой: у жилья моделей нет, поэтому «квартира Vision» — название дома.
    Неизвестная категория читает все, какие есть: «honda lead» без слова
    «скутер» — обычная формулировка, и модель в ней настоящая.
    """
    patterns = _ALL_PATTERNS if category is None else _PATTERNS_BY_CATEGORY.get(category, ())
    found = [
        (len(match.group(0)), match.start(), slug)
        for slug, pattern in patterns
        if (match := pattern.search(text)) is not None
    ]
    found.sort(key=lambda entry: (-entry[0], entry[1], entry[2]))
    return tuple(slug for _, _, slug in found)


def model_named_in(category: Category | None, text: str) -> str | None:
    """Самая конкретная из названных моделей. `None` — ни одной знакомой."""
    named = models_named_in(category, text)
    return named[0] if named else None


def model_brand(model: str | None) -> str | None:
    """Производитель модели: `lead` → `honda`. Незнакомая модель — `None`."""
    known = _MODELS.get(str(model or ""))
    return known.brand if known else None


_MOTORBIKE_BRAND_SET = frozenset(brand.lower() for brand in MOTORBIKE_BRANDS)


def brand_category(brand: str | None) -> Category | None:
    """Категория, следующая из марки: все марки рынка — мотобайковые.

    Симметрично `model_category`, но грубее и потому применяется ПОЗЖЕ: марка
    называет предмет менее однозначно, чем модель. На нашем рынке `MOTORBIKE_BRANDS`
    это марки мотобайков (honda, yamaha, sym, …), и «yamaha» без иных слов значит
    мотобайк — иначе на запрос «yamaha» категория оставалась пустой и в выдачу
    лезла даже квартира (realcheck 02.09.2026).

    Ложится ТОЛЬКО на пустое место, после слова и модели, — поэтому «сниму
    квартиру рядом с Honda» остаётся жильём: там категорию дало слово «квартиру».
    """
    return Category.MOTORBIKE if brand and brand.lower() in _MOTORBIKE_BRAND_SET else None


def model_category(model: str | None) -> Category | None:
    """Что за предмет назван моделью: `vision` → `motorbike`. Незнакомая — `None`.

    Имя модели называет предмет не менее однозначно, чем слово «скутер»:
    Vision, Lead, Exciter не бывают ничем другим. Поэтому клиента, назвавшего
    модель, спрашивать «что ищем?» незачем — вопрос там, где предмет уже назван,
    и есть та «тупизна», на которую жаловался владелец.
    """
    return _CATEGORY_BY_MODEL.get(str(model or ""))


def model_transmission(model: str | None) -> str | None:
    """Коробка, однозначно следующая из модели. `None` — не следует.

    `None` означает именно «вывести нельзя», а не «данных нет»: у электробайка
    коробки в этом смысле не существует, и приписать ему «автомат» значило бы
    отправить источнику фильтр, который электробайки исключает.
    """
    known = _MODELS.get(str(model or ""))
    return TRANSMISSION_BY_BODY.get(known.body) if known else None


def model_engine_cc(model: str | None) -> int | None:
    """Представительный объём модели в см³. `None` — электро либо модель незнакома.

    Тем же путём, что `model_brand`/`model_transmission`: знание живёт в таблице
    `motorbike_models`, а слои читают его отсюда. Число справочное и огрублённое
    (у модели ряд вариантов, здесь одно частое) — годится расставить лот по классу
    объёма, когда сам лот объёма не назвал, но текст лота всегда главнее (см.
    `relevance._wrong_engine`).
    """
    known = _MODELS.get(str(model or ""))
    return known.engine_cc if known else None


# ── Какими словами названа категория: слово рынка, марка или модель ──────────
# Одно знание на обе стороны воронки. Бесплатный гейт продавца
# (`pipeline/gate.py`) держал СВОЙ список марок и моделей — honda, vision, nouvo,
# sirius, winner, exciter, janus, — и он разошёлся с истиной: sym, kymco, lead,
# attila, pcx, click в нём не было. Терсовое «Sym Attila 50cc, 15тр» гейт
# отбрасывал как «без категории» ещё ДО базы, и никакая правка поиска потерянный
# лот уже не спасала. Теперь слова берутся отсюда, из тех же таблиц, что и поиск
# (`CATEGORY_TERMS`, `MOTORBIKE_BRANDS`, `motorbike_models`), и второй копии,
# которой есть куда разъехаться, больше нет.

_CYRILLIC_VOWELS = "аеёиоуыэюя"
_MIN_CATEGORY_STEM = 4


def _category_word_pattern(term: str) -> re.Pattern[str]:
    """Слово рынка → как его пишет продавец в объявлении.

    Русское существительное склоняется, и продавец пишет «аренда байков»,
    «скутеры», «квартиру», «студии». Хвост берётся не любой (`\\w*`), а только
    падежным окончанием существительного: любой хвост ловил «комнатная» в
    «1-комнатная квартира» как комнату и «великолепный» как велосипед, а целое
    слово без окончаний (прежнее правило для основ на согласную) не узнавало
    «байков» — замер 18.09.2026: «Аренда байков, доставка к квартире»
    становилась квартирой. Короткое слово («дом») по-прежнему ищется целиком:
    у трёхбуквенной основы любое окончание ловит чужое. Пробел значит «может
    быть, а может и не быть» — та же дисциплина, что у написаний модели и города.
    """
    head, _, tail = term.rpartition(" ")
    if _is_cyrillic(tail):
        vowel_end = tail[-1:].lower() in _CYRILLIC_VOWELS
        stem = tail[:-1] if vowel_end else tail
        # Основа на согласную склоняется и короткой («дом» → «дома», «доме»), но
        # у короткой окончания узкие: «домой» (наречие) и «домашний» не ловятся.
        # Короткая основа от слова на гласную («авто» → «авт») не склоняется
        # вовсе: её окончания съедают чужие слова.
        endings = _NOUN_ENDINGS if len(stem) >= _MIN_CATEGORY_STEM else _SHORT_ENDINGS
        if len(stem) >= _MIN_CATEGORY_STEM or (not vowel_end and len(stem) >= 3):
            base = re.escape(f"{head} {stem}" if head else stem).replace(r"\ ", r"\s*")
            return re.compile(rf"\b{base}(?:{endings})?\b", re.IGNORECASE)
    whole = re.escape(f"{head} {tail}" if head else tail).replace(r"\ ", r"\s*")
    return re.compile(rf"\b{whole}\b", re.IGNORECASE)


# Окончания русского существительного во всех падежах обоих чисел. Прилагательных
# («-ная», «-ный») здесь нет намеренно — ради этого список и закрыт.
_NOUN_ENDINGS = "ами|ями|ах|ях|ам|ям|ов|ев|ей|ой|ом|ем|ью|а|я|у|ю|ы|и|е|о"
_SHORT_ENDINGS = "ами|ах|ам|ов|ом|а|у|е|ы"


def _is_cyrillic(word: str) -> bool:
    return any("а" <= letter.lower() <= "я" or letter.lower() == "ё" for letter in word)


_CATEGORY_WORD_PATTERNS: tuple[tuple[Category, re.Pattern[str]], ...] = tuple(
    (category, _category_word_pattern(term))
    for category, langs in CATEGORY_TERMS.items()
    for terms in langs.values()
    for term in terms
)
_BRAND_RE = re.compile(r"\b(?:" + "|".join(MOTORBIKE_BRANDS) + r")\b", re.IGNORECASE)


# Признаки жилья, которых нет в `CATEGORY_TERMS` (там слова для поиска, а эти
# ищут плохо): «2 спальни», «1 bedroom», «phòng ngủ». В объявлении о квартире
# слова «квартира» часто нет вовсе — «Marina Suites — 2 спальни, вид на море», —
# и без этих признаков категорию давала парковка для байка в тексте.
_HOUSING_SIGNS = re.compile(
    r"\b(?:спал(?:ьн|ен)\w*|bedrooms?|phòng\s+ngủ|кв\.?\s*м|м²|m2"
    r"|пентхаус\w*|дуплекс\w*|апп?арт\w*|penthouse|duplex)(?!\w)",
    re.IGNORECASE,
)

_HOUSING = frozenset({Category.APARTMENT, Category.ROOM, Category.HOUSE})

# Число прямо перед словом: «2 комнаты», «2-х комнат», «три комнаты».
_BATHROOM_RE = re.compile(r"\bванн\w*[\s\-–]*$", re.IGNORECASE)
_APPLIANCE_RE = re.compile(r"\b(?:стиральн|посудомоечн|кофе|швейн)\w*[\s\-–]*$", re.IGNORECASE)

# Рекламный шаблон агентства в заголовке: один и тот же у квартир, домов и вилл
# («💵Снимите дом в Нячанге без комиссии.💵», а в тексте — «Квартира в аренду»).
# Аудит 18.09.2026: 142 карточки с таким заголовком, 138 из них — квартиры.
# Такой заголовок предмета не называет, решает текст.
_BOILERPLATE_TITLE_RE = re.compile(r"\bбез\s+(?:каких-либо\s+)?комисси", re.IGNORECASE)

_COUNTED_RE = re.compile(
    r"(?:\d|\bдв[еу]х?|\bтр[её]х?|\bтри|\bчетыр[её]х?|\bпят[иь])[\s\-–]*(?:х[\s\-]*)?$",
    re.IGNORECASE,
)

# Порядок категорий, когда предмет не назван в заголовке. Жильё впереди
# транспорта: в объявлении о квартире байк — это парковка или прокат рядом, а в
# объявлении о байке квартира почти не упоминается. Машина последней — «авто»
# в тексте почти всегда трансфер или парковка. Внутри жилья квартира главнее
# комнаты («2 комнаты» — это число комнат квартиры) и дома («для дома»).
_BODY_PRIORITY: tuple[Category, ...] = (
    Category.APARTMENT,
    Category.ROOM,
    Category.HOUSE,
    Category.MOTORBIKE,
    Category.BICYCLE,
    Category.CAR,
    Category.OTHER,
)


# Слово заголовка — буквы, перед которыми не стоит «#»: строка из одних
# хэштегов («#Нячанг #аренда #сдам») — рубрика чата, а предмет назван строкой
# ниже.
_TITLE_WORD_RE = re.compile(r"(?:^|[^#\w])[^\W\d_]{2,}")


def _title_end(text: str) -> int:
    """Конец заголовка: первой строки, где есть слово не из хэштега.

    Всё выше него (строки хэштегов) тоже считается заголовком: «#квартира» в
    рубрике — такое же прямое название предмета, как слово в первой строке.
    """
    position = 0
    skipped = False
    for line in text.split("\n"):
        if _TITLE_WORD_RE.search(line):
            if _BOILERPLATE_TITLE_RE.search(line):
                # Рекламный шаблон агентства — не заголовок: предмет назовёт текст.
                return 0
            # Заголовок не длиннее `_TITLE_MAX`: у поста из эмодзи-«заголовка»
            # и сплошного текста без переносов иначе заголовком стал бы весь
            # текст, и «парковка для авто» в середине решала бы категорию. Если
            # выше были строки-украшения, «заголовок» найден лишь в тексте, и
            # доверия к нему меньше — окно короче (аудит 18.09.2026:
            # «Пентхаус… 10 минут на байке» становилось байком).
            return position + min(len(line), _TITLE_FALLBACK if skipped else _TITLE_MAX)
        if line.strip():
            skipped = True
        position += len(line) + 1
    return len(text)


_TITLE_FALLBACK = 60


_TITLE_MAX = 120


def category_hints(text: str) -> list[Category]:
    """Какие категории названы в тексте объявления — в порядке уверенности.

    Возвращает СПИСОК: одно сообщение вправе назвать и байк, и жильё сразу.
    Гейт воронки берёт категорию карточки из первого элемента, поэтому порядок —
    это и есть решение, что продаётся:

    * предмет, названный в ЗАГОЛОВКЕ (первая непустая строка), — первым, и из
      нескольких в заголовке — названный раньше. Объявление начинается с того,
      что предлагает: «Продам Honda Lead, доставлю к квартире» — байк;
    * остальное — по `_BODY_PRIORITY`: жильё впереди транспорта, машина в конце.

    Пока порядок задавала только таблица слов (мотобайк первым), «Сдаётся
    квартира… рядом с Honda Nha Trang, парковка для байка» становилось
    мотобайком — 72 активные карточки на 18.09.2026. Чистая позиция по всему
    тексту была проверена на 6691 живой карточке и отвергнута: «2 комнаты»
    внутри квартиры делало комнату, «парковка для авто» — машину.

    Марка и модель — такое же упоминание байка, как слово. Своего списка слов
    здесь нет, кроме признаков жилья; мультиязычно ровно настолько, насколько
    мультиязычны сами таблицы.
    """
    first: dict[Category, int] = {}

    def seen(category: Category, position: int) -> None:
        if position < first.get(category, len(text) + 1):
            first[category] = position

    for category, pattern in _CATEGORY_WORD_PATTERNS:
        for match in pattern.finditer(text):
            before = text[max(0, match.start() - 40) : match.start()]
            if category is Category.ROOM and (
                _COUNTED_RE.search(before) or _BATHROOM_RE.search(before)
            ):
                # «2 комнаты» — число комнат квартиры или дома, «ванная комната»
                # — санузел; ни то ни другое не комната в аренду. Аудит
                # 18.09.2026: из 102 карточек, ставших комнатой, 63 были о
                # санузлах домов и квартир.
                continue
            if category is Category.CAR and _APPLIANCE_RE.search(before):
                # «стиральная машина» — удобство квартиры, не автомобиль: все 19
                # переходов «квартира → машина» в аудите были ею.
                continue
            seen(category, match.start())
            break
    housing = _HOUSING_SIGNS.search(text)
    if housing is not None and not first.keys() & _HOUSING:
        # Признак жилья — только когда вид жилья словом не назван: «Трёхэтажный
        # дом… 3 спальни» — дом, а не квартира.
        seen(Category.APARTMENT, housing.start())
    lowered = text.casefold()
    for slug in models_named_in(None, text):
        spot = lowered.find(slug.replace("_", " ").casefold())
        seen(_CATEGORY_BY_MODEL[slug], spot if spot >= 0 else len(text))
    brand = _BRAND_RE.search(text)
    if brand is not None:
        seen(Category.MOTORBIKE, brand.start())

    title_end = _title_end(text)
    in_title = sorted((c for c in first if first[c] < title_end), key=lambda c: first[c])
    in_body = sorted((c for c in first if first[c] >= title_end), key=_BODY_PRIORITY.index)
    return [*in_title, *in_body]
