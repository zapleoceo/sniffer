"""Паспорт → отбор и оценка карточки. Ни одной строки SQL.

Граница здесь та же, что во всём проекте: SQL живёт в `db/`, решение — тут.
Репозиторий умеет «дай карточки такого города, категории, дешевле такого-то и
не старше такого-то», а что считать «таким-то» и насколько находка хороша,
решает этот модуль.

Отдельно от `search/relevance.py` не по недосмотру. Тот ранжирует `RawItem` —
сырой текст из живого поиска, где цену приходится угадывать. Здесь `Listing` —
уже разобранная карточка со структурной ценой и атрибутами, и правила у неё
другие: дороже бюджета отбрасываем совсем, а не двигаем вниз, потому что в
подписке некому посмотреть и сказать «ну ладно, это близко».
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from math import exp

from sniffer.domain.match_filter import build_match_filter, ceiling_vnd
from sniffer.domain.passport import (
    HOUSING_PREFERENCES_KEY,
    Currency,
    Passport,
    engine_cc_bounds,
    with_default_attributes,
)
from sniffer.domain.records import Listing, MatchFilter
from sniffer.matching.attribute_match import conflicts, matches

# Насколько старая карточка ещё годится в подписку. Тот же порог, что у
# `verifier/liveness.py`: объявление старше двух недель чаще продано, чем нет.
MATCH_MAX_AGE_DAYS = 14
# Порог показа. Ниже — карточка формально подходит, но клиенту не полезна:
# подписка шлёт сама, без спроса, и цена ошибки здесь выше, чем в поиске.
MATCH_MIN_SCORE = 0.55
# Насколько новой карточка должна быть В МОМЕНТ слежения. Слежение обещает НОВОЕ: карточка
# с `posted_at` вчерашней давности, получившая свежий id при доборе архива или пересчёте,
# новой не является, и счёт оценки её не остановит — при цене в бюджете и совпавшем
# атрибуте он не падает ниже порога при любой давности (D10).
MONITOR_MAX_AGE = timedelta(hours=24)
# Свойства паспорта, чьё сравнение у слежения НЕ равенство: объём идёт полосой, направление
# — служебное, модель судится словами. Равенство по ним резало бы «от 250» и «200 ±25%» (D3).
_NOT_EQUALITY = frozenset({"engine_cc", "engine_cc_dir", "model"})


def filter_for(
    passport: Passport, *, usd_vnd: float | None = None, now: datetime | None = None
) -> MatchFilter | None:
    """Условия отбора по паспорту. `None` — подбирать не по чему.

    Без города и категории отбор превращается в «покажи всё подряд», а
    подписка на всё подряд — это спам, за который бота отключают в первые сутки.
    Остальное собирает тот же построитель, что у диалога: критерии двух путей одни.
    """
    if not passport.city or passport.category is None:
        return None
    moment = now or datetime.now(UTC)
    return build_match_filter(
        city=passport.city,
        category=passport.category.value,
        intent=passport.intent,
        ceiling=ceiling_vnd(passport.budget, usd_vnd),
        since=moment - timedelta(days=MATCH_MAX_AGE_DAYS),
        attributes=passport.attributes,
    )


def score(listing: Listing, passport: Passport, *, now: datetime | None = None) -> float:
    """Насколько карточка отвечает запросу. 0..1.

    Свежесть весит больше, чем в живом поиске: там клиент сам решил посмотреть
    сейчас, здесь мы будим его сами, и вчерашнее объявление — плохой повод.
    """
    moment = now or datetime.now(UTC)
    return round(
        0.45 * _freshness(listing, moment)
        + 0.35 * _price_fit(listing, passport)
        + 0.20 * _attribute_fit(listing, passport),
        4,
    )


def worth_sending(listing: Listing, passport: Passport, *, now: datetime | None = None) -> bool:
    moment = now or datetime.now(UTC)
    if _age(listing, moment) > MONITOR_MAX_AGE:
        return False
    wanted_model = str(passport.attributes.get("model") or "")
    if wanted_model and not _listing_has_model(listing, wanted_model):
        return False
    if _known_attribute_conflicts(listing, passport):
        return False
    return score(listing, passport, now=now) >= MATCH_MIN_SCORE


def _listing_has_model(listing: Listing, wanted: str) -> bool:
    actual = str(listing.attributes.get("model") or "")
    wanted_words = _model_words(wanted)
    if actual and _model_words(actual) == wanted_words:
        return True
    haystack = _model_words(f"{listing.title} {listing.summary}")
    return bool(wanted_words and re.search(rf"(?<!\w){re.escape(wanted_words)}(?!\w)", haystack))


def _model_words(value: str) -> str:
    return re.sub(r"[^\w]+", " ", value.casefold().replace("_", " ")).strip()


def _known_attribute_conflicts(listing: Listing, passport: Passport) -> bool:
    """Слежение не шлёт известное противоречие явному требованию.

    Требования те же, что у диалога: умолчание категории (байк без слова «электро» —
    бензиновый) и полоса объёма вместо равенства. Неизвестное свойство не мешает.
    """
    wanted_all = with_default_attributes(
        passport.category.value if passport.category else None, passport.attributes
    )
    low, high = engine_cc_bounds(wanted_all.get("engine_cc"), wanted_all.get("engine_cc_dir"))
    actual_cc = listing.attributes.get("engine_cc")
    if isinstance(actual_cc, int | float) and not isinstance(actual_cc, bool):
        if (low is not None and actual_cc < low) or (high is not None and actual_cc > high):
            return True
    for field, wanted in wanted_all.items():
        if field in _NOT_EQUALITY or field == HOUSING_PREFERENCES_KEY:
            continue
        actual = listing.attributes.get(field)
        if conflicts(field, actual, wanted, passport.attributes):
            return True
    return False


def _age(listing: Listing, now: datetime) -> timedelta:
    posted = listing.posted_at
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    return now - posted


def _freshness(listing: Listing, now: datetime) -> float:
    posted = listing.posted_at
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    hours = max(0.0, (now - posted).total_seconds() / 3600)
    return exp(-hours / 48)


def _price_fit(listing: Listing, passport: Passport) -> float:
    """Цена внутри бюджета — единица, за бюджетом — ноль, без цены — половина.

    Плавного спада, как в живом поиске, здесь нет намеренно: отбор в базе уже
    отсёк дорогое, а карточка без цены не «наполовину подходит» — про неё
    просто ничего не известно, и половина честнее любой другой оценки.
    """
    if passport.budget.max is None:
        return 0.5
    if listing.price_amount is None:
        return 0.5
    return 1.0


def _attribute_fit(listing: Listing, passport: Passport) -> float:
    """Доля совпавших атрибутов паспорта. Пустой паспорт — половина."""
    wanted = {
        k: v
        for k, v in passport.attributes.items()
        if k not in _NOT_EQUALITY and k != HOUSING_PREFERENCES_KEY
    }
    if not wanted:
        return 0.5
    have = listing.attributes
    matched = sum(
        1
        for field, value in wanted.items()
        if field in have and matches(field, have[field], value, wanted)
    )
    # Отсутствующий атрибут не считаем несовпадением: минимальная карточка их
    # ещё не извлекает, и штрафовать за то, чего воронка не умеет, нечестно.
    known = sum(1 for field in wanted if field in have)
    if not known:
        return 0.5
    return matched / known


def needs_usd_rate(passport: Passport) -> bool:
    """Бюджет в долларах с потолком: без курса он не становится потолком в донгах.

    Объявления написаны в донгах, и отбор по долларовому бюджету без курса вырождается
    в «любая цена подходит» (`ceiling_vnd` отдаёт `None`, а `_price_fit` ставит
    единицу любой известной цене). Для разового поиска это терпимо — клиент видит
    выдачу и решает сам. Подписка шлёт без спроса: дорогое она бы отправила как
    «идеально в бюджете». Поэтому тот, кто зовёт подбор для подписки, обязан по этому
    признаку дождаться курса, а не звать `filter_for` без него.

    Условие то же, что в ветке USD у `domain.match_filter.ceiling_vnd`: тест держит их вместе.
    """
    budget = passport.budget
    return budget.max is not None and budget.currency is Currency.USD
