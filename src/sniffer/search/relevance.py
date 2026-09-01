"""Детерминированный отбор находок по паспорту до показа карточек."""

from __future__ import annotations

from datetime import UTC, datetime
from math import exp

from sniffer.domain.passport import Budget, Currency, Passport
from sniffer.search.plan import SearchPlan, SearchTask
from sniffer.search.vocabulary import attribute_phrases
from sniffer.sources.base import RawItem

PRICE_OVER_BUDGET = 1.30


def with_vnd_budget(plan: SearchPlan, passport: Passport, rate: float | None) -> SearchPlan:
    """Передать источникам VND-бюджет, если клиент указал сумму в USD."""
    budget = _budget_vnd(passport.budget, rate)
    if budget is None:
        return plan
    tasks = [_with_budget(task, budget) for task in plan.tasks]
    return plan.model_copy(update={"tasks": tasks})


def rank_items(
    passport: Passport,
    items: list[RawItem],
    *,
    usd_vnd: float | None,
    now: datetime | None = None,
) -> list[RawItem]:
    """Порядок: свежесть, бюджет, совпадение атрибутов, продавец.

    Вариант дороже бюджета не исчезает бесследно: когда точных вариантов нет,
    близкий лот полезнее пустого ответа, но всегда идёт после подходящих.
    """
    moment = now or datetime.now(UTC)
    return sorted(items, key=lambda item: _score(item, passport, usd_vnd, moment), reverse=True)


def _with_budget(task: SearchTask, budget: dict[str, float | str]) -> SearchTask:
    return task.model_copy(update={"params": {**task.params, "budget": budget}})


def _budget_vnd(budget: Budget, rate: float | None) -> dict[str, float | str] | None:
    if budget.currency is Currency.VND:
        multiplier = 1.0
    elif budget.currency is Currency.USD and rate is not None:
        multiplier = rate
    else:
        return None
    if budget.min is None and budget.max is None:
        return None
    return {
        "min": (budget.min or 0) * multiplier,
        "max": (budget.max or 10_000_000_000) * multiplier,
        "currency": Currency.VND.value,
        "period": budget.period.value,
    }


def _score(item: RawItem, passport: Passport, usd_vnd: float | None, now: datetime) -> float:
    return (
        0.35 * _freshness(item, now)
        + 0.30 * _price_fit(item, passport.budget, usd_vnd)
        + 0.25 * _attribute_fit(item, passport)
        + 0.10 * 0.5
    )


def _freshness(item: RawItem, now: datetime) -> float:
    if item.posted_at is None:
        return 0.0
    posted = item.posted_at if item.posted_at.tzinfo else item.posted_at.replace(tzinfo=UTC)
    age_hours = max(0.0, (now - posted).total_seconds() / 3600)
    return exp(-age_hours / 48)


def _price_fit(item: RawItem, budget: Budget, usd_vnd: float | None) -> float:
    ceiling = _budget_ceiling_vnd(budget, usd_vnd)
    if ceiling is None:
        return 0.5
    if item.price_vnd is None:
        return 0.25
    if item.price_vnd <= ceiling:
        return 1.0
    return max(0.0, (PRICE_OVER_BUDGET - item.price_vnd / ceiling) / (PRICE_OVER_BUDGET - 1))


def _budget_ceiling_vnd(budget: Budget, usd_vnd: float | None) -> float | None:
    if budget.max is None:
        return None
    if budget.currency is Currency.VND:
        return budget.max
    if budget.currency is Currency.USD and usd_vnd is not None:
        return budget.max * usd_vnd
    return None


def _attribute_fit(item: RawItem, passport: Passport) -> float:
    if passport.category is None or not passport.attributes:
        return 0.5
    haystack = f"{item.title} {item.text}".casefold()
    matched = 0
    for field, value in passport.attributes.items():
        phrases = (
            attribute_phrases(passport.category, {field: value}, "ru")
            + attribute_phrases(passport.category, {field: value}, "vi")
            + attribute_phrases(passport.category, {field: value}, "en")
        )
        if any(phrase.casefold() in haystack for phrase in phrases):
            matched += 1
    # Отсутствующее слово не считаем несовпадением: Chotot уже мог отфильтровать
    # это свойство структурным полем, а оно не обязано повторяться в заголовке.
    return 0.5 + 0.5 * matched / len(passport.attributes)
