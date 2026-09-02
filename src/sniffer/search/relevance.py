"""Детерминированный отбор находок по паспорту до показа карточек."""

from __future__ import annotations

from datetime import UTC, datetime
from math import exp

from sniffer.domain.passport import Budget, Currency, Passport
from sniffer.search.plan import SearchPlan, SearchTask
from sniffer.search.vocabulary import attribute_phrases, models_named_in
from sniffer.sources.base import RawItem

PRICE_OVER_BUDGET = 1.30

# Возраст, после которого лот уходит из живой выдачи. Это не тот порог, что в
# `verifier/liveness` (14 дней): там решается, когда ПРЕДУПРЕДИТЬ, и карточка
# честно говорит «объявлению N дней, могло быть продано» — лот старше двух
# недель чаще продан, чем нет, но всё ещё бывает жив. Здесь решается другое: с
# какого возраста показывать лот ВМЕСТО более свежего значит врать. Знание
# разное, поэтому и число своё, а не производное от чужого.
#
# Вдвое дальше порога предупреждения: пометка «могло быть продано» к этому
# моменту верна дольше, чем лот вообще был живым. Такая жёсткость допустима
# ровно потому, что подстрахована — если после отсева не осталось ничего,
# старое показывается всё равно (`rank_items`). Живой повод: 02.09.2026 в
# выдаче всплыл лот 59-дневной давности.
LIVE_MAX_AGE_DAYS = 28


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
    """Порядок и отсев перед показом.

    Порядок: свежесть, бюджет, совпадение атрибутов, продавец. Вариант дороже
    бюджета не исчезает бесследно: когда точных вариантов нет, близкий лот
    полезнее пустого ответа, но всегда идёт после подходящих.

    Отсев появился после жалобы 02.09.2026: на «нужен скутер honda lead» бот
    отдал три Airblade и лот 59-дневной давности. Сортировка одна такую выдачу
    не лечит — она ставит мусор ниже, а показываем мы первые пять, и когда
    лучшего нет, первыми пятью оказывается мусор.

    Отсев двухступенчатый, и ступени отменяются по-разному:

    * **чужая модель** — не низкий балл, а брак выдачи (spec-v2, 3.2): клиент
      назвал Lead, значит Airblade это не «похуже», а не тот предмет. Ступень не
      отменяется: если Lead на рынке нет, честнее пустой ответ, который прямо
      советует «попробуйте без марки» (`bot/conversation.NOTHING_FOUND`), чем
      пять Airblade — ровно то, на что жаловался владелец;
    * **совсем старое** — догадка о живости, а не факт о предмете, и лот старше
      порога всё-таки бывает жив. Поэтому ступень отменяется, когда после неё
      пусто: пустая выдача хуже слабой, а карточка сама скажет «объявлению N
      дней, могло быть продано» (`bot/cards.py`).

    Порога по итоговому баллу здесь нет намеренно, хотя в подписке он есть
    (`matching.MATCH_MIN_SCORE`). Там `posted_at` у карточки обязателен, а в
    живом поиске он необязателен, и `_freshness` отдаёт ноль ОДИНАКОВО лоту без
    даты и лоту двухмесячной давности. Балльный порог отсекал бы не старое, а
    неизвестное — то есть законную карточку с честной пометкой «дата публикации
    неизвестна» (spec-v2, 3.3).
    """
    moment = now or datetime.now(UTC)
    ranked = sorted(items, key=lambda item: _score(item, passport, usd_vnd, moment), reverse=True)
    asked_for = [item for item in ranked if not _other_model(item, passport)]
    return [item for item in asked_for if not _too_old(item, moment)] or asked_for


def _other_model(item: RawItem, passport: Passport) -> bool:
    """Лот НЕ той модели, которую назвал клиент.

    Фильтр клиентский, потому что структурного поля модели у доски нет: у
    Chotot есть `motorbiketype`, `motorbikecapacity` и `motorbikebrand` — тип,
    объём и марка, — и это всё (spec-v2, 4.1.1). Марки мало: на «honda lead»
    `motorbikebrand=1` отдаёт все 26 Хонд Нячанга, а среди них Airblade, Vision
    и Wave. Именем модели в `q` доски это тоже не решается — `q` складывается с
    фильтрами через И и гасит их в ноль.

    Лот, не назвавший модель вовсе, чужим не считается — по тому же правилу, что
    и у прочих атрибутов: отсутствующее слово это неизвестность, а не
    несовпадение.
    """
    wanted = str(passport.attributes.get("model") or "")
    if not wanted:
        return False
    named = models_named_in(passport.category, f"{item.title} {item.text}")
    return bool(named) and wanted not in named


def _too_old(item: RawItem, now: datetime) -> bool:
    """Лот, который почти наверняка продан.

    Лот без даты старым НЕ считается: про него ничего не известно, а
    неизвестность — не возраст. Именно поэтому отсев идёт по дате, а не по
    итоговому баллу: балл у этих двух случаев одинаковый.
    """
    posted = _posted_utc(item)
    if posted is None:
        return False
    return (now - posted).days > LIVE_MAX_AGE_DAYS


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
    posted = _posted_utc(item)
    if posted is None:
        return 0.0
    age_hours = max(0.0, (now - posted).total_seconds() / 3600)
    return exp(-age_hours / 48)


def _posted_utc(item: RawItem) -> datetime | None:
    """Наивную метку источника считаем UTC: иначе вычитание уронит всю выдачу."""
    posted = item.posted_at
    if posted is None:
        return None
    return posted if posted.tzinfo else posted.replace(tzinfo=UTC)


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
    haystack = f"{item.title} {item.text}"
    matched = sum(
        _mentions(passport, field, value, haystack) for field, value in passport.attributes.items()
    )
    # Отсутствующее слово не считаем несовпадением: Chotot уже мог отфильтровать
    # это свойство структурным полем, а оно не обязано повторяться в заголовке.
    return 0.5 + 0.5 * matched / len(passport.attributes)


def _mentions(passport: Passport, field: str, value: object, haystack: str) -> bool:
    """Назван ли этот атрибут в тексте лота.

    Модель — не свойство, а имя, и слов рынка у неё нет. Ищется она тем же
    знанием, которым узнаётся в запросе клиента: иначе «model» попадало бы в
    знаменатель доли совпавших атрибутов, никогда не попадая в числитель, и
    добавление модели в паспорт молча роняло бы балл КАЖДОГО лота.
    """
    if field == "model":
        return str(value) in models_named_in(passport.category, haystack)
    phrases = [
        phrase
        for lang in ("ru", "vi", "en")
        for phrase in attribute_phrases(passport.category, {field: value}, lang)
    ]
    folded = haystack.casefold()
    return any(phrase.casefold() in folded for phrase in phrases)
