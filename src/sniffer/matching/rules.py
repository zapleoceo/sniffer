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

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import exp

from sniffer.domain.passport import Currency, Passport
from sniffer.domain.records import Listing, MatchFilter

# Насколько старая карточка ещё годится в подписку. Тот же порог, что у
# `verifier/liveness.py`: объявление старше двух недель чаще продано, чем нет.
MATCH_MAX_AGE_DAYS = 14
# Порог показа. Ниже — карточка формально подходит, но клиенту не полезна:
# подписка шлёт сама, без спроса, и цена ошибки здесь выше, чем в поиске.
MATCH_MIN_SCORE = 0.55


def filter_for(
    passport: Passport, *, usd_vnd: float | None = None, now: datetime | None = None
) -> MatchFilter | None:
    """Условия отбора по паспорту. `None` — подбирать не по чему.

    Без города и категории отбор превращается в «покажи всё подряд», а
    подписка на всё подряд — это спам, за который бота отключают в первые сутки.
    """
    if not passport.city or passport.category is None:
        return None
    moment = now or datetime.now(UTC)
    return MatchFilter(
        city=passport.city,
        category=passport.category.value,
        deal_type=passport.intent.value if passport.intent else None,
        max_price_vnd=_ceiling(passport, usd_vnd),
        since=moment - timedelta(days=MATCH_MAX_AGE_DAYS),
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
    return score(listing, passport, now=now) >= MATCH_MIN_SCORE


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
    wanted = passport.attributes
    if not wanted:
        return 0.5
    have = listing.attributes
    matched = sum(1 for field, value in wanted.items() if str(have.get(field, "")) == str(value))
    # Отсутствующий атрибут не считаем несовпадением: минимальная карточка их
    # ещё не извлекает, и штрафовать за то, чего воронка не умеет, нечестно.
    known = sum(1 for field in wanted if field in have)
    if not known:
        return 0.5
    return matched / known


def _ceiling(passport: Passport, usd_vnd: float | None) -> Decimal | None:
    budget = passport.budget
    if budget.max is None:
        return None
    if budget.currency is Currency.VND:
        return Decimal(str(budget.max))
    if budget.currency is Currency.USD and usd_vnd is not None:
        return Decimal(str(budget.max * usd_vnd))
    # Валюта известна, курса нет: врать про потолок нельзя, лучше не сужать.
    return None
