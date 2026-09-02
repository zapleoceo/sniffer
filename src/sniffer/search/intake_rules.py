"""Формулировка клиента → паспорт, без модели.

Работает всегда: когда брокер лежит, когда исчерпан дневной cap и когда ключа
просто нет. Разбирает три вещи, которые определяют выдачу, — намерение,
категорию и город, — плюс бюджет, марку, модель и коробку передач.

Часть фактов не сказана словом, а следует из другого: категория и коробка
следуют из модели, марка — тоже. Выведенное ложится ТОЛЬКО на пустое место:
сказанное клиентом главнее любой таблицы.

Почему это не дублирует `pipeline.gate` и `search.vocabulary`. Гейт читает
объявление продавца и обязан ловить бренды и модели; словарь рынка отвечает на
вопрос «какими словами торгуют». Здесь третье знание: какими словами клиент
формулирует запрос — с падежными окончаниями («квартиру», «в Нячанге»),
разговорным «двушка» и глаголами со своей стороны сделки. Совпадение текста
местами есть, знание разное.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sniffer.domain.passport import Category, Intent, Passport, PassportStatus
from sniffer.search.budget_rules import parse_budget
from sniffer.search.engine_size import read_engine_size, without_engine_cc
from sniffer.search.market_terms import ALL_CITY_NAMES, ATTRIBUTE_TERMS, LangTerms
from sniffer.search.motorbike_models import MOTORBIKE_BRANDS
from sniffer.search.vocabulary import (
    brand_category,
    city_variants,
    model_brand,
    model_category,
    model_named_in,
    model_transmission,
)

# Порядок значим: побеждает первое совпадение. «Ищу квартиру в аренду» — это
# аренда, а не покупка, поэтому глаголы сделки идут раньше общего «ищу».
_INTENT_RULES: tuple[tuple[Intent, re.Pattern[str]], ...] = (
    (Intent.SELL, re.compile(r"\b(?:прода(?:м|ю|ть)|sell|for\s?sale)\b", re.IGNORECASE)),
    (Intent.RENT_OUT, re.compile(r"\b(?:сда(?:м|ю|ть)|cho\s?thuê)\b", re.IGNORECASE)),
    (
        Intent.RENT,
        re.compile(
            r"\b(?:сни(?:му|мать)|снять|аренд\w*|в\s?аренду|rent|to\s?let|thuê)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Intent.BUY,
        re.compile(r"\b(?:куп(?:лю|ить)|покупк\w*|buy|purchase)\b", re.IGNORECASE),
    ),
)

# «Ищу», «нужен», «подскажите» намеренно НЕ считаются намерением: они говорят,
# что клиент ищет, но не говорят, покупает он или снимает. Различает их
# категория — квартиру в Нячанге снимают, скутер берут себе.

# Клиент пишет с окончаниями, поэтому `\w*` почти везде. Порядок тоже значим:
# «байк» разговорно мотоцикл, а не велосипед, и мотоциклы проверяются первыми.
_CATEGORY_RULES: tuple[tuple[Category, re.Pattern[str]], ...] = (
    (
        Category.MOTORBIKE,
        re.compile(
            r"\b(?:скутер\w*|байк\w*|мотобайк\w*|мотоцикл\w*|мопед\w*"
            r"|scooter|motorbike|moto|xe\s?máy|xe\s?ga)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Category.BICYCLE,
        re.compile(r"\b(?:велосипед\w*|велик\w*|bicycle|xe\s?đạp)\b", re.IGNORECASE),
    ),
    (
        Category.APARTMENT,
        re.compile(
            r"\b(?:квартир\w*|апартамент\w*|апарт|студи\w*|однушк\w*|двушк\w*|тр[её]шк\w*"
            r"|apartment|studio|căn\s?hộ|chung\s?cư)\b",
            re.IGNORECASE,
        ),
    ),
    (Category.ROOM, re.compile(r"\b(?:комнат\w*|room|phòng)\b", re.IGNORECASE)),
    (
        Category.HOUSE,
        re.compile(r"\b(?:дом|дома|домик\w*|вилл\w*|house|villa|nhà)\b", re.IGNORECASE),
    ),
    (
        Category.CAR,
        re.compile(r"\b(?:машин\w*|автомобил\w*|авто|car|ô\s?tô)\b", re.IGNORECASE),
    ),
)

# Марка приезжает в `attributes` и оттуда попадает первым запросом в шаблонный
# план: пишется она одинаково на всех трёх языках рынка.
#
# Здесь ТОЛЬКО производители. Модели лежат отдельной таблицей
# (`motorbike_models`), и разделение это не косметическое: пока оба списка были
# одним, побеждало первое совпадение regex — «honda lead» читалось как «honda»,
# модель терялась, план уходил по всем Хондам, и клиент, просивший Lead,
# получал Airblade (жалоба владельца 02.09.2026).
_BRAND_RE = re.compile(r"\b(?:" + "|".join(MOTORBIKE_BRANDS) + r")\b", re.IGNORECASE)
_BRAND_ALIASES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("honda", re.compile(r"\bхонд(?:а|у|ы|е|ой)?\b", re.IGNORECASE)),
    ("yamaha", re.compile(r"\bямах(?:а|у|и|е|ой)?\b", re.IGNORECASE)),
)

# Буквы, которыми кончается русское слово в именительном падеже и не кончается в
# косвенном: «механика» → «механику». Прибавляемое окончание добирает `\w*`, а
# ЗАМЕНЯЕМОЕ — нет, поэтому у кириллического слова хвост отбрасывается. Тот же
# приём, что у «й» в названии города; здесь список шире, потому что слова
# словаря рынка — существительные и прилагательные, а не одни топонимы.
_CYRILLIC_ENDING = "аяоёеиыуюэьй"
_MIN_STEM = 4


@dataclass(frozen=True, slots=True)
class _AttributeRule:
    """Одно написание одного значения атрибута: чей он, что значит и как пишется."""

    category: Category
    attribute: str
    value: str
    term: str
    pattern: re.Pattern[str]


def _term_pattern(term: str) -> re.Pattern[str]:
    """Слово рынка → как его пишет клиент: «автомат» ловит и «на автомате»."""
    head, _, tail = term.rpartition(" ")
    if _cyrillic(tail) and len(tail) > _MIN_STEM and tail[-1] in _CYRILLIC_ENDING:
        tail = tail[:-1]
    # Пробел в термине значит «пробел здесь может быть, а может не быть» — та же
    # дисциплина, что у написаний модели («air blade» и «airblade» — одно имя).
    escaped = re.escape(f"{head} {tail}" if head else tail).replace(r"\ ", r"\s*")
    return re.compile(rf"\b{escaped}\w*", re.IGNORECASE)


def _cyrillic(word: str) -> bool:
    return any("а" <= letter.lower() <= "я" or letter.lower() == "ё" for letter in word)


def _rules(
    category: Category, attribute: str, values: dict[str, LangTerms]
) -> list[_AttributeRule]:
    return [
        _AttributeRule(category, attribute, value, term, _term_pattern(term))
        for value, langs in values.items()
        for terms in langs.values()
        for term in terms
    ]


# Слова значений атрибутов НЕ переписаны здесь вторым списком: «автомат»,
# «вариатор», «tay ga», «côn tay» — то же знание, которым коробка ищется в тексте
# объявления, и разъехаться двум копиям негде. Разница только в окончаниях, и её
# добирает `_term_pattern`.
#
# Плоский кортеж, отсортированный по ДЛИНЕ написания: «полуавтомат» содержит
# «автомат», «semi-automatic» содержит «automatic», и решать, что назвал клиент,
# обязано написание, а не порядок строк в таблице (то же правило, что у моделей).
_ATTRIBUTE_RULES: tuple[_AttributeRule, ...] = tuple(
    sorted(
        (
            rule
            for category, attributes in ATTRIBUTE_TERMS.items()
            for attribute, values in attributes.items()
            for rule in _rules(category, attribute, values)
        ),
        key=lambda rule: (-len(rule.term), rule.term),
    )
)


def _city_pattern(slug: str) -> re.Pattern[str]:
    """Город ищем написаниями из справочника, а не слагом: клиент пишет «в Нячанге»."""
    alternatives = "|".join(
        re.escape(_city_stem(name)).replace(r"\ ", r"\s?") for name in city_variants(slug)
    )
    # `\w*` — падежное окончание: «Нячанг», «Нячанге», «Нячанга».
    return re.compile(rf"\b(?:{alternatives})\w*", re.IGNORECASE)


def _city_stem(name: str) -> str:
    """Отрезаем «й»: в косвенном падеже его нет («Ханой» → «в Ханое»).

    Только «й» и только на конце: остальные названия рынка склоняются
    прибавлением окончания, которое добирает `\\w*`.
    """
    return name.removesuffix("й") if len(name) > 3 else name


# Узнаём и те города, где не ищем: город, оставшийся неузнанным, подставляется
# городом по умолчанию, и запрос про Хойан становится неотличим от повтора
# запроса про Нячанг. Отказ искать — ответ, молчаливая подмена города — нет.
_CITY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (slug, _city_pattern(slug)) for slug in ALL_CITY_NAMES
)

# Жильё в Нячанге снимают, а не покупают: иностранцу с туристической визой
# купить квартиру нельзя в принципе. Для транспорта симметрично — берут себе.
_RENTED_CATEGORIES = (Category.APARTMENT, Category.ROOM, Category.HOUSE)

MAX_QUERY_CHARS = 500


def parse_query(text: str, *, default_city: str = "") -> Passport:
    """Разбор без ввода-вывода: те же слова дают тот же паспорт всегда."""
    query = " ".join(text.split())[:MAX_QUERY_CHARS]
    intent = detect_intent(query)
    said = detect_category(query)
    city = detect_city(query)
    # Модель ищется тем, что назвал КЛИЕНТ: у жилья моделей нет, и «сниму
    # квартиру Vision» — это название дома. Выведи категорию раньше, и таблица
    # начала бы читать сама себя.
    model = detect_model(query, said)
    # Марка ищется по СКАЗАННОЙ клиентом категории, а не по выведенной: она сама
    # участвует в выводе категории ниже, и брать выведенную значило бы дать
    # таблице прочитать саму себя. Для мотобайка это одно и то же (все модели
    # мотобайковые), но порядок обязан быть честным.
    brand = detect_brand(query, said)
    # Категория следует из модели ИЛИ марки, но ложится только на пустое место —
    # та же дисциплина, что у коробки: сказанное клиентом главнее выведенного.
    # Модель точнее марки, поэтому раньше неё; все марки рынка мотобайковые, и
    # «yamaha» без иных слов — мотобайк (иначе в выдачу лезла даже квартира).
    category = said or model_category(model) or brand_category(brand)
    if intent is None:
        intent = Intent.RENT if category in _RENTED_CATEGORIES else Intent.BUY

    attributes: dict[str, Any] = {}
    # Объём двигателя вынимается ПЕРВЫМ и вырезается из текста: «200 кубиков»
    # и «до 200» отличаются одним словом после числа, и разбор бюджета обязан
    # его не увидеть. Живой отказ 01.09.2026 — «200 кубиков» стали бюджетом в
    # 200000 VND (семь долларов), и поиск, разумеется, не нашёл ничего.
    engine = read_engine_size(query)
    if engine is not None:
        attributes["engine_cc"] = engine.value
        # Направление несём, только когда оно не точка: «exact» — прежнее
        # поведение (полоса ±band в relevance), и хранить его незачем, иначе
        # каждый разбор без направления менял бы форму паспорта.
        if engine.direction != "exact":
            attributes["engine_cc_dir"] = engine.direction
    budget = parse_budget(without_engine_cc(query), intent=intent)
    if model:
        attributes["model"] = model
    if brand:
        attributes["brand"] = brand
    transmission = detect_transmission(query, category)
    if transmission:
        attributes["transmission"] = transmission
    papers = detect_papers(query, category)
    if papers:
        attributes["papers"] = papers

    known_city = city or default_city or None
    return Passport(
        intent=intent,
        category=category,
        city=known_city,
        budget=budget,
        attributes=with_model_facts(attributes),
        raw_query=query,
        confidence=_confidence(category, city, budget.max),
        missing_fields=_missing(category, city, budget.max),
        status=PassportStatus.READY if category and known_city else PassportStatus.DRAFT,
    )


def detect_intent(text: str) -> Intent | None:
    for intent, pattern in _INTENT_RULES:
        if pattern.search(text):
            return intent
    return None


def detect_category(text: str) -> Category | None:
    for category, pattern in _CATEGORY_RULES:
        if pattern.search(text):
            return category
    return None


def detect_brand(text: str, category: Category | None = None) -> str | None:
    """Марка техники. Пишется одинаково на всех трёх языках рынка.

    Названа прямо — берём названное. Названа только модель — марка следует из
    таблицы: «лид» без слова «honda» это всё равно Honda, и спрашивать об этом
    клиента незачем.
    """
    found = _BRAND_RE.search(text)
    if found is not None:
        return found.group(0).lower()
    for brand, pattern in _BRAND_ALIASES:
        if pattern.search(text):
            return brand
    return model_brand(detect_model(text, category))


def detect_model(text: str, category: Category | None = None) -> str | None:
    """Модель техники: «honda lead» → `lead`, «нужен лид» → тоже `lead`.

    Порядок совпадений здесь не решает ничего (`models_named_in` выбирает по
    длине написания) — в отличие от прежнего разбора, где марка и модель лежали
    одним списком и побеждало первое совпадение.

    Категорию спрашивает таблица, а не ветка в коде: у жилья моделей нет, и
    «квартира Vision» — это название дома. Неизвестная категория читает весь
    модельный ряд: «honda lead» без слова «скутер» — обычная формулировка.

    Незнакомая модель остаётся неузнанной, и это осознанный предел: таблица
    короткая намеренно, а неузнанная модель возвращает прежнее поведение —
    поиск по марке.
    """
    return model_named_in(category, text)


def category_of(text: str) -> Category | None:
    """Категория текста — по слову, а если слова нет, по названной модели.

    «Honda Lead 110 2008» не содержит слова «скутер» — только имя модели, — но
    Lead бывает лишь у мотобайка. Сторона запроса и сторона лота ОБЯЗАНЫ читать
    категорию одинаково: `parse_query` выводит её из модели («хочу вижн» →
    motorbike), а отбор выдачи раньше этого не делал, и лот, назвавший только
    модель, для запроса о жилье выглядел «неизвестной категорией» и проходил
    фильтр (realcheck 03.09.2026 — Honda Lead в выдаче студий).

    Модель ищется уже с учётом слова: у листинга «студия Vision Tower» категория
    названа словом (apartment), и до модели дело не доходит — «Vision» там имя
    дома, а не Honda. Та же защита, что у `parse_query`: вывод категории из
    модели не даёт таблице читать саму себя.
    """
    said = detect_category(text)
    return said or model_category(detect_model(text, said))


def detect_transmission(text: str, category: Category | None = None) -> str | None:
    """Коробка, названная словом: «скутер автомат», «на механике», «tay ga», «xe số».

    Слова берутся из словаря рынка (`ATTRIBUTE_TERMS`) — второго списка здесь
    нет намеренно: это то же знание, которым коробка ищется в тексте объявления,
    и две копии однажды разъедутся. Разъезд не гипотетический: пока разбор
    ответов держал свой список, «xe số» значило в нём механику, а в словаре
    рынка — полуавтомат, и заметить это по тексту было нельзя.

    Категорию спрашивает таблица, а не ветка в коде: у жилья коробки нет,
    поэтому «квартира со стиральной машиной автомат» ничего не заполняет.
    Неизвестная категория читает все, какие есть, — та же дисциплина, что у
    модели: на вопрос «автомат или механика?» клиент отвечает одним словом, и
    категории в этом слове нет.
    """
    return _attribute_named_in(category, "transmission", text)


def detect_papers(text: str, category: Category | None = None) -> str | None:
    """Документы, названные словом: «блюкарт», «синяя карта», «giấy tờ đầy đủ».

    Мягкий сигнал, а не фильтр, и это осознанно: продавец без блюкарта об этом
    обычно молчит, «нет документов» в объявлении почти не пишут. Жёсткий отсев по
    документам оставил бы клиента с пустой выдачей вместо той, где документные
    лоты просто выше. Поэтому `papers` в паспорте поднимает балл лота с блюкартом
    (`relevance._attribute_fit`), но чужую карточку не выбрасывает — в отличие от
    марки и коробки. Кому нужно строго «только с документами» — это deal_breaker,
    отдельный механизм (passport.md).

    Слова — из того же словаря рынка (`PAPERS_WORDS` через `ATTRIBUTE_TERMS`),
    которым документы узнаются и в тексте объявления: разъехаться двум копиям
    негде.
    """
    return _attribute_named_in(category, "papers", text)


def _attribute_named_in(category: Category | None, attribute: str, text: str) -> str | None:
    """Значение атрибута, названное в тексте. Длинное написание побеждает."""
    for rule in _ATTRIBUTE_RULES:
        if rule.attribute != attribute or (category is not None and rule.category is not category):
            continue
        if rule.pattern.search(text):
            return rule.value
    return None


def with_model_facts(attributes: dict[str, Any]) -> dict[str, Any]:
    """Что следует из названной модели: марка и коробка передач.

    Выводится ТОЛЬКО на пустое место — та же дисциплина, что у объёма
    двигателя: сказанное клиентом главнее выведенного. «Lead на механике» —
    заведомо несуществующий байк, но спорить с клиентом не наше дело: он увидит
    выдачу и поправит её кнопкой.

    Живёт одной функцией, потому что путей к паспорту два — правила и ответ
    модели. Вывод, сделанный только в одном из них, — дефект, заметный лишь на
    боевом пути с работающим брокером.
    """
    model = str(attributes.get("model") or "")
    if not model:
        return attributes
    derived = {"brand": model_brand(model), "transmission": model_transmission(model)}
    return {**{key: value for key, value in derived.items() if value is not None}, **attributes}


def detect_city(text: str) -> str | None:
    for slug, pattern in _CITY_RULES:
        if pattern.search(text):
            return slug
    return None


def _confidence(category: Category | None, city: str | None, budget_max: float | None) -> float:
    """Уверенность эвристики принципиально скромная: она читает слова, не смысл."""
    found = sum(value is not None for value in (category, city, budget_max))
    return round(0.25 + 0.15 * found, 2)


def _missing(category: Category | None, city: str | None, budget_max: float | None) -> list[str]:
    """Чего клиент не сказал. Подставленное по умолчанию считается несказанным."""
    checks = (("category", category), ("city", city), ("budget.max", budget_max))
    return [field for field, value in checks if value is None]
