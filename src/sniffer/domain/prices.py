"""Одна цена объявления из свободного текста.

Это вход живого Telegram-поиска и обработки архива. Что считать суммой, решает
`price_facts` (контекст и словарь письма в `price_vocab`); здесь — какая из
найденных сумм и есть цена объявления. Прежний вход ``price_hint`` остался:
он нужен там, где известны только текст и донги.
"""

from __future__ import annotations

from dataclasses import replace

from sniffer.domain.price_bounds import price_bounds
from sniffer.domain.price_facts import MAX_PLAUSIBLE_VND, PriceFact, parse_prices
from sniffer.domain.price_vocab import TO_MONTH

__all__ = [
    "MAX_PLAUSIBLE_VND",
    "PriceFact",
    "choose_price",
    "parse_price",
    "parse_prices",
    "price_hint",
]

# Пометка сильнее: «Цена:» надёжнее значка денег, значок надёжнее голой суммы,
# а итоговая строка агрегатора — последняя надежда, когда в тексте цены нет.
_RANK = {"label": 0, "weak": 1, "money": 1, "text": 2, "footer": 3}
# Только для проверки границ у сумм в долларах, не для показа клиенту.
_ROUGH_USD_VND = 25_000


def _monthly(fact: PriceFact) -> float:
    return fact.amount * TO_MONTH.get(fact.period or "", 1)


def _vnd(fact: PriceFact, *, rent: bool) -> float:
    base = _monthly(fact) if rent else fact.amount
    return base * (_ROUGH_USD_VND if fact.currency == "USD" else 1)


def _resolved(fact: PriceFact, bounds: tuple[int, int] | None, *, rent: bool) -> PriceFact | None:
    """Факт внутри границ; сокращённое число получает тот масштаб, что в границы входит."""
    if bounds is None:
        return None if fact.bare else fact
    scales = (1_000, 1_000_000) if fact.bare else (1,)
    for scale in scales:
        candidate = (
            replace(fact, amount=round(fact.value * scale), bare=False) if fact.bare else fact
        )
        if bounds[0] <= _vnd(candidate, rent=rent) <= bounds[1]:
            return candidate
    return None


def choose_price(
    facts: list[PriceFact], *, rent: bool | None = None, bounds: tuple[int, int] | None = None
) -> PriceFact | None:
    """Одна цена объявления из найденных сумм.

    Сначала границы правдоподобия (если известны), потом срок: аренде нужна
    помесячная цена, продаже — цена без срока, и суточную цену байка с месячной
    путать нельзя — это разница на порядок. Потом пометка и наименьшая: каталог
    цен по этажам и срокам сводится к «от 9 млн», остальные уходят в ``up_to``.
    """
    pool = [fact for fact in facts if fact.currency == "VND"] or facts
    pool = [r for fact in pool if (r := _resolved(fact, bounds, rent=bool(rent))) is not None]
    if not pool:
        return None
    fits = {True: (None, "month"), False: (None,)}.get(rent) if rent is not None else None
    group = [fact for fact in pool if fits is None or fact.period in fits]
    if not group and rent is not False:
        # Аренда без помесячной цены довольствуется посуточной или понедельной.
        # Продаже срок не идёт вовсе: «6 млн/мес» в объявлении о продаже дома —
        # это доход от аренды соседнего, а не цена дома.
        group = pool
    if not group:
        return None
    primary = min(group, key=lambda fact: (_RANK[fact.source], _monthly(fact)))
    peers = [
        fact
        for fact in group
        if (fact.currency, fact.period, fact.source)
        == (primary.currency, primary.period, primary.source)
    ]
    ceiling = max(max(fact.amount, fact.up_to or 0) for fact in peers)
    return replace(primary, up_to=ceiling if ceiling > primary.amount else None)


def parse_price(
    text: str, *, category: str | None = None, deal_type: str | None = None
) -> PriceFact | None:
    """Цена объявления; категория и сторона сделки уточняют срок и границы."""
    rent = None if deal_type not in {"rent_out", "sell"} else deal_type == "rent_out"
    return choose_price(parse_prices(text), rent=rent, bounds=price_bounds(category, deal_type))


def price_hint(text: str) -> tuple[str, int | None]:
    """Вернуть написанную цену и её значение в VND либо честное ``None``.

    Вход живого Telegram-поиска: ему нужны только донги, потому что `price_vnd`
    по определению донги. Сумма в долларах здесь не подходит.
    """
    fact = parse_price(text)
    if fact is None or fact.currency != "VND":
        return "", None
    return fact.raw, fact.amount
