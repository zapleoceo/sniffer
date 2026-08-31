"""Формулировка клиента → паспорт запроса.

Один вызов LLM, как и у планировщика, и та же дисциплина отказа: модель
недоступна, ответила мусором или ключа просто нет — берётся эвристический
разбор. Бот без модели должен понимать хуже, но понимать.

Ответ модели не заменяет эвристику целиком, а дополняет её: поле, которое
модель оставила пустым, берётся из разбора по правилам. Так «до 400 долларов»
не теряется из-за того, что провайдер поленился заполнить бюджет.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import httpx
import structlog

from sniffer.broker.client import BrokerClient, BrokerError
from sniffer.config import get_settings
from sniffer.domain.passport import (
    Budget,
    Category,
    Currency,
    Intent,
    Passport,
    PassportStatus,
    PricePeriod,
)
from sniffer.search.intake_rules import parse_query
from sniffer.search.planner import StructuredCaller
from sniffer.search.vocabulary import ALL_CITY_NAMES

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
brand — марка техники, если названа."""


class QueryIntake:
    def __init__(self, broker: StructuredCaller | None = None) -> None:
        # Брокер создаётся лениво: собрать разбор должно быть можно и без
        # настроенного окружения.
        self._broker = broker

    async def parse(self, text: str) -> Passport:
        settings = get_settings()
        rules = parse_query(text, default_city=settings.default_city)

        if self._broker is None and not settings.broker_project_key.strip():
            # Ключа нет — идти некуда. Молча ждать таймаут на каждом сообщении
            # клиента дороже, чем сразу ответить по правилам.
            log.info("intake.rules_only", reason="брокер не настроен")
            return rules

        try:
            payload = await self._ask(text)
        except (BrokerError, httpx.HTTPError, TimeoutError) as exc:
            log.warning("intake.broker_failed", kind=type(exc).__name__, error=str(exc))
            return rules

        merged = merge(rules, payload)
        log.info(
            "intake.ready",
            category=merged.category,
            city=merged.city,
            budget_max=merged.budget.max,
        )
        return merged

    async def _ask(self, text: str) -> dict[str, Any]:
        broker = self._broker or BrokerClient()
        self._broker = broker
        return await broker.structured(
            f"Запрос клиента: {text}",
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
        "required": ["intent", "category", "city", "budget_max", "currency", "period", "brand"],
        "properties": {
            "intent": _enum(Intent),
            "category": _enum(Category),
            "city": {"type": "string", "enum": ["", *ALL_CITY_NAMES]},
            "budget_max": {"type": "string", "description": "число без валюты, пусто если нет"},
            "currency": _enum(Currency),
            "period": _enum(PricePeriod),
            "brand": {"type": "string"},
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
    attributes = dict(rules.attributes)
    brand = str(payload.get("brand") or "").strip().lower()
    if brand:
        attributes["brand"] = brand

    category = _member(Category, payload.get("category")) or rules.category
    city = str(payload.get("city") or "").strip() or rules.city
    return rules.model_copy(
        update={
            "intent": _member(Intent, payload.get("intent")) or rules.intent,
            "category": category,
            "city": city,
            "budget": budget,
            "attributes": attributes,
            # Модель прочитала смысл, а не слова, — это заметно точнее правил.
            "confidence": 0.8,
            "missing_fields": [
                field
                for field, value in (("category", category), ("budget.max", budget.max))
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
