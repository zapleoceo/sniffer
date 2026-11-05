"""Формулировка клиента → паспорт запроса.

Один вызов LLM, как и у планировщика, и та же дисциплина отказа: модель
недоступна, ответила мусором или ключа просто нет — берётся эвристический
разбор. Бот без модели должен понимать хуже, но понимать.

Ответ модели не заменяет эвристику целиком, а дополняет её: поле, которое
модель оставила пустым, берётся из разбора по правилам. Так «до 400 долларов»
не теряется из-за того, что провайдер поленился заполнить бюджет.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

import httpx
import structlog

from sniffer.broker.client import BrokerClient, BrokerError
from sniffer.broker.usage import default_usage_sink
from sniffer.config import get_settings
from sniffer.domain.housing_rooms import read_bedrooms, read_total_rooms
from sniffer.domain.passport import (
    Budget,
    Category,
    Currency,
    Intent,
    Passport,
    PassportStatus,
    PricePeriod,
)
from sniffer.search.engine_size import MAX_CC, MIN_CC
from sniffer.search.intake_rules import (
    _query_districts,
    grounded_model,
    parse_query,
    with_model_facts,
)
from sniffer.search.market_terms import ALL_CITY_NAMES
from sniffer.search.planner import StructuredCaller
from sniffer.search.refinements import negates_budget_floor_removal

log = structlog.get_logger(__name__)

INTAKE_MAX_TOKENS = 600

SYSTEM_PROMPT = """Ты разбираешь запрос клиента о частных объявлениях в
Вьетнаме в структуру. Отвечай только тем, что клиент действительно сказал или
что однозначно следует из его слов: незаполненное поле честнее выдуманного.

city — латинский слаг города из перечисления схемы: Нячанг → nha_trang,
Дананг → da_nang, Хойан → hoi_an. Города, которого в перечислении нет, не
подменяй похожим: оставь поле пустым.
budget_max — число без пробелов и валюты; «10 млн донгов» → 10000000.
currency — валюта суммы; «млн» без валюты во Вьетнаме означает донги.
brand — производитель, если назван: Honda, Yamaha, Suzuki, Piaggio, SYM.
model — модель, если названа: Lead, Air Blade, Vision, Wave, Exciter, Vespa,
Zoomer или другая. Список примеров не закрытый: верни точное имя клиента,
даже если раньше его не встречал. Не додумывай модель, которой нет в запросе.
Марка и модель — РАЗНЫЕ поля: «honda lead» это brand=honda и model=lead.
Не отбрасывай часть названия и не расширяй точный запрос до одной марки."""


SYSTEM_PROMPT += (
    " For housing requests, distinguish total rooms from bedrooms. Return exact "
    "district_evidence, total_rooms_evidence, and bedrooms_evidence copied from the user's "
    "text; rooms_minimum is yes only when the user states a lower bound. For an edit, "
    "budget_min_action is remove only when the user explicitly removes the lower price "
    "bound; budget_min_evidence must quote the exact words requesting removal. "
    "An omitted bound means keep. Use the current search as context, not as facts "
    "newly stated by the user. Use an "
    "empty string when absent or ambiguous. Never infer a district from the city."
)


class QueryIntake:
    def __init__(self, broker: StructuredCaller | None = None) -> None:
        # Брокер создаётся лениво: собрать разбор должно быть можно и без
        # настроенного окружения.
        self._broker = broker

    def default_new_city(self, passport: Passport) -> Passport:
        """Apply the configured city only after choosing to open a new search."""
        if passport.city is not None:
            return passport
        city = get_settings().default_city
        return passport.model_copy(
            update={
                "city": city,
                "missing_fields": [field for field in passport.missing_fields if field != "city"],
                "status": PassportStatus.READY
                if passport.category and city
                else PassportStatus.DRAFT,
            }
        )

    async def parse(self, text: str, *, for_edit: bool = False) -> Passport:
        parsed, _ = await self._parse(text, for_edit=for_edit, current=None)
        return parsed

    async def parse_edit(self, text: str, current: Passport) -> tuple[Passport, bool]:
        """Parse one contextual edit; return an explicit lower-bound removal separately."""
        return await self._parse(text, for_edit=True, current=current)

    async def _parse(
        self, text: str, *, for_edit: bool, current: Passport | None
    ) -> tuple[Passport, bool]:
        settings = get_settings()
        # Город по умолчанию подставляется, а не спрашивается: клиент рынка
        # Нячанга по умолчанию в Нячанге, и вопрос «в каком городе?» на «нужен
        # байк» — та самая лишняя ступень допроса, на которую жаловался владелец
        # (журнал 04.09.2026). Дананг он назовёт словом, необслуживаемый ловит
        # `is_served`. Search-first: до выдачи спрашиваем только предмет.
        rules = parse_query(text, default_city="" if for_edit else settings.default_city)

        if (rules.is_ready() or (for_edit and rules.category is not None)) and rules.attributes.get(
            "model"
        ):
            # The exact named item is already executable. A paid semantic pass
            # cannot add a question here, but can add seconds and overwrite a
            # correct model, so agent reasoning is reserved for ambiguity.
            log.info("intake.rules_only", reason="точная модель уже разобрана")
            return rules, False

        if self._broker is None and not settings.broker_project_key.strip():
            # Ключа нет — идти некуда. Молча ждать таймаут на каждом сообщении
            # клиента дороже, чем сразу ответить по правилам.
            log.info("intake.rules_only", reason="брокер не настроен")
            return rules, False

        try:
            payload = await self._ask(text, current=current)
        except (BrokerError, httpx.HTTPError, TimeoutError) as exc:
            log.warning("intake.broker_failed", kind=type(exc).__name__, error=str(exc))
            return rules, False

        merged = merge(rules, payload)
        if for_edit:
            # A semantic guess must not turn an omitted city into a city edit.
            merged = merged.model_copy(update={"city": rules.city})
        log.info(
            "intake.ready",
            category=merged.category,
            city=merged.city,
            budget_max=merged.budget.max,
        )
        removes_floor = (
            current is not None
            and isinstance(payload, dict)
            and payload.get("budget_min_action") == "remove"
            and bool(str(payload.get("budget_min_evidence") or "").strip())
            and str(payload.get("budget_min_evidence") or "").strip() in text
            and rules.budget.min is None
            and not negates_budget_floor_removal(text)
        )
        return merged, removes_floor

    async def _ask(self, text: str, *, current: Passport | None = None) -> dict[str, Any]:
        broker = self._broker or BrokerClient(usage=default_usage_sink)
        self._broker = broker
        context = (
            "\nТекущий поиск: "
            + json.dumps(
                current.model_dump(
                    mode="json",
                    include={"intent", "category", "city", "districts", "budget", "attributes"},
                ),
                ensure_ascii=False,
            )
            if current is not None
            else ""
        )
        return await broker.structured(
            f"Запрос клиента: {text}{context}",
            schema=intake_schema(),
            schema_name="query_passport",
            system=SYSTEM_PROMPT,
            max_tokens=INTAKE_MAX_TOKENS,
        )


def intake_schema() -> dict[str, Any]:
    """Строгая схема разбора.

    Все поля строковые и все обязательные: strict json_schema не допускает
    необязательных, а «не знаю» модель выражает пустой строкой. Числа тоже
    приходят строкой — провайдеры возвращают то `400`, то `"400 USD"`, и
    разобрать строку надёжнее, чем спорить о типе.

    Перечисление городов — `ALL_CITY_NAMES`, а не только обслуживаемые: закрепи
    здесь два города, и модель физически не сможет сказать, что клиент назвал
    третий. Отказ искать в Хойане — ответ; молча подставленный Нячанг — нет.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "intent",
            "category",
            "city",
            "budget_max",
            "currency",
            "period",
            "brand",
            "model",
            "engine_cc",
            "district_evidence",
            "total_rooms_evidence",
            "rooms_minimum",
            "bedrooms_evidence",
            "budget_min_action",
            "budget_min_evidence",
        ],
        "properties": {
            "intent": _enum(Intent),
            "category": _enum(Category),
            "city": {"type": "string", "enum": ["", *ALL_CITY_NAMES]},
            "district_evidence": {"type": "string"},
            "total_rooms_evidence": {"type": "string"},
            "rooms_minimum": {"type": "string", "enum": ["", "yes", "no"]},
            "bedrooms_evidence": {"type": "string"},
            "budget_min_action": {"type": "string", "enum": ["", "keep", "remove"]},
            "budget_min_evidence": {"type": "string"},
            "budget_max": {"type": "string", "description": "число без валюты, пусто если нет"},
            "currency": _enum(Currency),
            "period": _enum(PricePeriod),
            "brand": {"type": "string"},
            # Модель — своё поле рядом с маркой, потому что без него модели
            # некуда деться: она приезжала в `brand`, и «honda lead» становилось
            # запросом по всем Хондам (жалоба владельца 02.09.2026).
            #
            # Перечисления здесь НЕТ, в отличие от города, и это ровно тот же
            # урок, прочитанный с другой стороны. У городов список закрыт, и
            # модель физически не может назвать несуществующий. Модельный ряд
            # рынка закрытым списком не бывает: закрепи в `enum` четырнадцать
            # имён — и на «Honda SH» модель выберет ближайшее из них, то есть
            # соврёт увереннее, чем промолчала бы. Незнакомое имя принимается
            # только при буквальном подтверждении в исходной фразе клиента.
            "model": {
                "type": "string",
                "description": "модель техники, если названа; пусто если нет",
            },
            # Своё поле под объём двигателя — и это лечение, а не удобство.
            # Пока его не было, единственным числовым полем оставался
            # `budget_max`, и модель клала туда «200 кубиков». Она не виновата:
            # когда для числа есть ровно одно место, оно туда и попадает.
            "engine_cc": {
                "type": "string",
                "description": "объём двигателя в кубических сантиметрах, пусто если не назван",
            },
        },
    }


def merge(rules: Passport, payload: Any) -> Passport:
    """Ответ модели поверх разбора по правилам. Пустое поле модели — не ответ."""
    if not isinstance(payload, dict):
        log.warning("intake.payload_not_object", kind=type(payload).__name__)
        return rules

    budget = Budget(
        min=rules.budget.min,
        max=_number(payload.get("budget_max")) or rules.budget.max,
        currency=_member(Currency, payload.get("currency")) or rules.budget.currency,
        period=_member(PricePeriod, payload.get("period")) or rules.budget.period,
    )
    category = _member(Category, payload.get("category")) or rules.category
    attributes = dict(rules.attributes)
    brand = str(payload.get("brand") or "").strip().lower()
    if brand:
        attributes["brand"] = brand
    # Ответ модели проходит через тот же разбор написаний, что и текст клиента:
    # она пишет то «Honda Lead», то «air blade», то «SH» — а в паспорте обязан
    # лежать слаг из таблицы, иначе фильтр выдачи не с чем сверять. Незнакомое
    # Незнакомое имя сохраняется только если оно буквально присутствует в
    # запросе. Так открытый модельный ряд не становится whitelist, а выдуманная
    # модель из ответа провайдера по-прежнему не может сузить поиск.
    model = grounded_model(payload.get("model"), rules.raw_query, category)
    if model:
        attributes["model"] = model
    # Правила уже могли вынуть объём регулярным выражением — и это точнее
    # модели, поэтому её ответ идёт вторым и только на пустое место.
    if "engine_cc" not in attributes:
        engine_cc = _digits(payload.get("engine_cc"))
        if engine_cc is not None and MIN_CC <= engine_cc <= MAX_CC:
            attributes["engine_cc"] = engine_cc

    city = str(payload.get("city") or "").strip() or rules.city
    district_quote = str(payload.get("district_evidence") or "").strip()
    bedrooms_quote = str(payload.get("bedrooms_evidence") or "").strip()
    rooms_quote = str(payload.get("total_rooms_evidence") or "").strip()
    districts = rules.districts
    if not districts and district_quote and district_quote in rules.raw_query:
        districts = _query_districts(district_quote, city)
    if "bedrooms" not in attributes and bedrooms_quote and bedrooms_quote in rules.raw_query:
        if (bedrooms := read_bedrooms(bedrooms_quote)) is not None:
            attributes["bedrooms"] = bedrooms
    if "rooms" not in attributes and rooms_quote and rooms_quote in rules.raw_query:
        if (rooms := read_total_rooms(rooms_quote)) is not None:
            attributes["rooms"] = rooms
            if payload.get("rooms_minimum") == "yes":
                attributes["rooms_min"] = True
    return rules.model_copy(
        update={
            "intent": _member(Intent, payload.get("intent")) or rules.intent,
            "category": category,
            "city": city,
            "districts": districts,
            "budget": budget,
            # Модель могла приехать только от LLM — тогда марку и коробку из неё
            # выводить всё равно надо, и делает это та же функция, что в
            # правилах: иначе вывод работал бы ровно на том пути, где брокер лёг.
            "attributes": with_model_facts(attributes),
            # Модель прочитала смысл, а не слова, — это заметно точнее правил.
            "confidence": 0.8,
            "missing_fields": [
                field
                for field, value in (
                    ("category", category),
                    ("city", city),
                    ("budget.max", budget.max),
                )
                if value is None
            ],
            "status": PassportStatus.READY if category and city else PassportStatus.DRAFT,
        }
    )


def _enum(enum: type[StrEnum]) -> dict[str, Any]:
    return {"type": "string", "enum": ["", *(member.value for member in enum)]}


def _member[Member: StrEnum](enum: type[Member], value: Any) -> Member | None:
    try:
        return enum(str(value).strip())
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    try:
        number = float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _digits(value: object) -> int | None:
    """Число из ответа модели. Она возвращает то `200`, то `"200 cc"`."""
    text = "".join(char for char in str(value or "") if char.isdigit())
    return int(text) if text else None
