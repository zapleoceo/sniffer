"""Формулировка клиента → паспорт, без модели.

Работает всегда: когда брокер лежит, когда исчерпан дневной cap и когда ключа
просто нет. Разбирает три вещи, которые определяют выдачу, — намерение,
категорию и город, — плюс бюджет, марку и модель.

Почему это не дублирует `pipeline.gate` и `search.vocabulary`. Гейт читает
объявление продавца и обязан ловить бренды и модели; словарь рынка отвечает на
вопрос «какими словами торгуют». Здесь третье знание: какими словами клиент
формулирует запрос — с падежными окончаниями («квартиру», «в Нячанге»),
разговорным «двушка» и глаголами со своей стороны сделки. Совпадение текста
местами есть, знание разное.
"""

from __future__ import annotations

import re
from typing import Any

from sniffer.domain.passport import Category, Intent, Passport, PassportStatus
from sniffer.search.budget_rules import parse_budget
from sniffer.search.engine_size import read_engine_cc, without_engine_cc
from sniffer.search.market_terms import ALL_CITY_NAMES
from sniffer.search.motorbike_models import MOTORBIKE_BRANDS
from sniffer.search.vocabulary import (
    city_variants,
    model_brand,
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
    category = detect_category(query)
    city = detect_city(query)
    if intent is None:
        intent = Intent.RENT if category in _RENTED_CATEGORIES else Intent.BUY

    attributes: dict[str, Any] = {}
    # Объём двигателя вынимается ПЕРВЫМ и вырезается из текста: «200 кубиков»
    # и «до 200» отличаются одним словом после числа, и разбор бюджета обязан
    # его не увидеть. Живой отказ 01.09.2026 — «200 кубиков» стали бюджетом в
    # 200000 VND (семь долларов), и поиск, разумеется, не нашёл ничего.
    engine_cc = read_engine_cc(query)
    if engine_cc is not None:
        attributes["engine_cc"] = engine_cc
    budget = parse_budget(without_engine_cc(query), intent=intent)
    model = detect_model(query, category)
    if model:
        attributes["model"] = model
    brand = detect_brand(query, category)
    if brand:
        attributes["brand"] = brand

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
