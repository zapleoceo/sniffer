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
    """
    return tuple(
        (
            model.slug,
            re.compile(
                r"\b(?:"
                + "|".join(re.escape(name).replace(r"\ ", r"\s*") for name in model.spellings)
                + r")\b",
                re.IGNORECASE,
            ),
        )
        for model in models
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

    Русское существительное с гласной на конце склоняется сменой окончания
    («студия» → «студию»), поэтому у достаточно длинной основы берётся `\\w*`.
    Короткое или оканчивающееся на согласную слово ищется целиком: иначе
    «дом\\w*» поймал бы «домашний», «авт\\w*» — «автомат», «велик\\w*» —
    «великолепный». Пробел значит «может быть, а может и не быть» — та же
    дисциплина, что у написаний модели и города.
    """
    head, _, tail = term.rpartition(" ")
    if tail[-1:].lower() in _CYRILLIC_VOWELS and len(tail) - 1 >= _MIN_CATEGORY_STEM:
        stem = re.escape(f"{head} {tail[:-1]}" if head else tail[:-1]).replace(r"\ ", r"\s*")
        return re.compile(rf"\b{stem}\w*", re.IGNORECASE)
    whole = re.escape(f"{head} {tail}" if head else tail).replace(r"\ ", r"\s*")
    return re.compile(rf"\b{whole}\b", re.IGNORECASE)


_CATEGORY_WORD_PATTERNS: tuple[tuple[Category, re.Pattern[str]], ...] = tuple(
    (category, _category_word_pattern(term))
    for category, langs in CATEGORY_TERMS.items()
    for terms in langs.values()
    for term in terms
)
_BRAND_RE = re.compile(r"\b(?:" + "|".join(MOTORBIKE_BRANDS) + r")\b", re.IGNORECASE)


def category_hints(text: str) -> list[Category]:
    """Какие категории названы в тексте — словом рынка, маркой или моделью.

    Возвращает СПИСОК: одно сообщение вправе назвать и байк, и жильё сразу
    («продам скутер, сдам квартиру»). Порядок стабильный — сначала категории,
    названные словом (`CATEGORY_TERMS`, все языки рынка), потом та, что следует
    из марки или модели (`MOTORBIKE_BRANDS`, `motorbike_models`); у нас все марки
    мотобайковые. Порядок не случаен: гейт воронки берёт категорию карточки из
    первого элемента, и предмет, названный прямым словом, главнее выведенного из
    имени модели.

    Своего списка слов здесь нет — знание переиспользовано целиком, разъезжаться
    нечему. Мультиязычно ровно настолько, насколько мультиязычны сами таблицы.
    """
    found: list[Category] = []
    for category, pattern in _CATEGORY_WORD_PATTERNS:
        if pattern.search(text):
            found.append(category)
    for slug in models_named_in(None, text):
        found.append(_CATEGORY_BY_MODEL[slug])
    if _BRAND_RE.search(text):
        found.append(Category.MOTORBIKE)
    return list(dict.fromkeys(found))
