"""Паспорт запроса — центральная сущность продукта.

Из него растёт всё: запрос к базе, ключевые слова живого поиска, фильтр
подписки, критерии ранжирования. Описание полей: docs/passport.md.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Intent(StrEnum):
    BUY = "buy"
    RENT = "rent"
    SELL = "sell"
    RENT_OUT = "rent_out"


_COUNTERPART_INTENT: dict[Intent, str] = {
    Intent.BUY: "sell",
    Intent.RENT: "rent_out",
    Intent.SELL: "wanted",
    Intent.RENT_OUT: "wanted",
}


def counterpart_deal_type(intent: Intent | None) -> str | None:
    """Тип объявления на противоположной стороне сделки клиента."""
    return _COUNTERPART_INTENT.get(intent) if intent is not None else None


class Category(StrEnum):
    MOTORBIKE = "motorbike"
    BICYCLE = "bicycle"
    CAR = "car"
    APARTMENT = "apartment"
    ROOM = "room"
    HOUSE = "house"
    OTHER = "other"


# Жильё в Нячанге снимают, а не покупают: иностранцу с туристической визой
# купить квартиру нельзя в принципе. Знание одно на обе стороны рынка, поэтому
# лежит в домене, а не в двух списках: клиент, написавший «квартиру» без
# глагола, хочет снять (разбор запроса), а объявление о квартире без глагола
# сделки — предложение сдать (конвейер архива). Пока конвейер держал своё
# умолчание «продажа», 2707 из 3714 карточек жилья за 28 дней (замер
# 12.09.2026) значились продажей при тексте про аренду, и арендатор по стороне
# сделки не находил ничего.
RENTED_CATEGORIES: frozenset[Category] = frozenset(
    {Category.APARTMENT, Category.ROOM, Category.HOUSE}
)


def default_deal_type(category: Category | None) -> str:
    """Сторона объявления, когда глагола сделки в его тексте нет."""
    return "rent_out" if category in RENTED_CATEGORIES else "sell"


# Полоса допуска объёма двигателя, когда клиент назвал точку, а не границу:
# «200 кубиков» — это про класс мотоцикла, 175 и 250 клиент назовёт тем же
# поиском, 700 — нет. Одно число на отбор выдачи (`search/relevance.py`) и на
# отбор в каталоге (`sources/chat_directory.py` → SQL): разойдись они, карточка
# проходила бы одну границу и отсекалась другой.
ENGINE_CC_BAND = 0.25


def engine_cc_bounds(engine_cc: object, direction: object) -> tuple[int | None, int | None]:
    """Границы объёма по названному числу и направлению: «от 250», «до 125», «200»."""
    if isinstance(engine_cc, bool) or not isinstance(engine_cc, int) or engine_cc <= 0:
        return None, None
    if direction == "min":
        return engine_cc, None
    if direction == "max":
        return None, engine_cc
    return int(engine_cc * (1 - ENGINE_CC_BAND)), int(engine_cc * (1 + ENGINE_CC_BAND))


class Currency(StrEnum):
    USD = "USD"
    VND = "VND"
    EUR = "EUR"
    RUB = "RUB"


class PricePeriod(StrEnum):
    ONCE = "once"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class PassportStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    ACTIVE = "active"
    PAUSED = "paused"
    FULFILLED = "fulfilled"


class Budget(BaseModel):
    min: float | None = None
    max: float | None = None
    currency: Currency | None = None
    period: PricePeriod = PricePeriod.MONTH


class Passport(BaseModel):
    """Неизменяем: правка поля создаёт новую версию, а не переписывает эту."""

    intent: Intent | None = None
    category: Category | None = None
    city: str | None = None
    districts: list[str] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    # Набор зависит от категории (CATEGORY_ATTRIBUTES). Плоский JSONB —
    # добавление атрибута не требует миграции.
    attributes: dict[str, Any] = Field(default_factory=dict)
    must_have: list[str] = Field(default_factory=list)
    deal_breakers: list[str] = Field(default_factory=list)
    timeframe_from: date | None = None
    timeframe_to: date | None = None
    raw_query: str = ""
    confidence: float = 0.0
    missing_fields: list[str] = Field(default_factory=list)
    status: PassportStatus = PassportStatus.DRAFT

    def is_ready(self) -> bool:
        """Готов к подбору: есть намерение, категория и город."""
        return bool(self.intent and self.category and self.city)


# Какие атрибуты имеют смысл для категории. Валидация на уровне приложения,
# а не схемы БД — иначе каждый новый атрибут это миграция.
CATEGORY_ATTRIBUTES: dict[Category, tuple[str, ...]] = {
    Category.MOTORBIKE: (
        "transmission",
        "engine_cc",
        # Направление объёма: min|max — граница, отсутствие — точка (±band).
        # «от 250»/«250 минимум» → лот с cc<250 противоречит (search/engine_size).
        "engine_cc_dir",
        "year_min",
        # Марка и модель — разные атрибуты: у «honda lead» первое honda, второе
        # lead. Пока модели не было, она приезжала в `brand`, и запрос уходил по
        # всем Хондам сразу (docs/passport.md, «Марка и модель»).
        "brand",
        "model",
        "condition",
        "papers",
        "delivery",
        "test_ride",
    ),
    Category.APARTMENT: (
        "rooms",
        "area_m2",
        "floor",
        "furnished",
        "air_conditioner",
        "washing_machine",
        "pool",
        "gym",
        "elevator",
        "balcony",
        "sea_view",
        "pets_allowed",
        "min_term_months",
        "deposit_months",
        "utilities_included",
    ),
    Category.ROOM: (
        "area_m2",
        "floor",
        "furnished",
        "air_conditioner",
        "shared",
        "min_term_months",
        "deposit_months",
        "utilities_included",
    ),
}

# Доля кандидатов, которую отсекает поле. Стартовые эвристики: после P1
# считаются SQL по фактическому распределению listings, и набор вопросов
# начинает подстраиваться под рынок сам.
FIELD_INFORMATIVENESS: dict[Category, dict[str, float]] = {
    Category.MOTORBIKE: {
        "budget.max": 0.45,
        "attributes.transmission": 0.30,
        "attributes.condition": 0.15,
        "attributes.brand": 0.10,
    },
    Category.APARTMENT: {
        "budget.max": 0.40,
        "attributes.rooms": 0.25,
        "districts": 0.20,
        "attributes.furnished": 0.10,
    },
}

MAX_CLARIFYING_QUESTIONS = 3

# Обратная связь на карточках вправе спросить на один вопрос больше: три —
# защита от допроса ДО выдачи, а здесь клиент сам нажал кнопку и ждёт
# уточнения. Потолок абсолютный, а не «на один больше уже заданных»: иначе
# каждое нажатие поднимало бы его ещё на один, и правило превратилось бы в
# «сколько нажмёшь, столько и спросим».
MAX_FEEDBACK_QUESTIONS = MAX_CLARIFYING_QUESTIONS + 1


def next_questions(passport: Passport, limit: int = MAX_CLARIFYING_QUESTIONS) -> list[str]:
    """Какие поля спросить: только те, что реально сужают выдачу.

    Допрос из десяти вопросов убивает конверсию сильнее, чем неточная выдача,
    поэтому берём максимум `limit` самых информативных незаполненных полей.
    Показанная выдача уточняет запрос лучше вопроса: человеку проще сказать
    «дорого», глядя на пять карточек, чем назвать бюджет в пустоту.
    """
    if passport.category is None:
        return ["category"]

    weights = FIELD_INFORMATIVENESS.get(passport.category, {})
    unfilled = [field for field in weights if not has_value(passport, field)]
    unfilled.sort(key=lambda field: weights[field], reverse=True)
    return unfilled[:limit]


def has_value(passport: Passport, path: str) -> bool:
    """Заполнено ли поле по пути вида `budget.max` / `attributes.transmission`."""
    head, _, tail = path.partition(".")
    value = getattr(passport, head, None)
    if tail:
        value = value.get(tail) if isinstance(value, dict) else getattr(value, tail, None)
    return value not in (None, "", [], {})
