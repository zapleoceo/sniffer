"""Одна цена объявления из свободного текста.

Это вход живого Telegram-поиска и обработки архива. Что считать суммой, решает
`price_facts` (контекст и словарь письма в `price_vocab`); здесь — какая из
найденных сумм и есть цена объявления. Прежний вход ``price_hint`` остался:
он нужен живому поиску, где нужны только донги и написанная цена.
"""

from __future__ import annotations

from dataclasses import replace

from sniffer.domain.price_bounds import price_bounds
from sniffer.domain.price_facts import PriceFact, parse_prices
from sniffer.domain.price_numbers import MAX_PLAUSIBLE_VND
from sniffer.domain.price_vocab import RENT_LABEL_RE, SELL_LABEL_RE, TO_MONTH

__all__ = [
    "MAX_PLAUSIBLE_VND",
    "PriceFact",
    "choose_price",
    "fits_budget",
    "parse_price",
    "parse_prices",
    "price_hint",
]

# Пометка сильнее: «Цена:» в начале фразы надёжнее значка денег и метки посреди
# фразы, а они надёжнее голой суммы. Итоговую строку агрегатора не читаем вовсе:
# она повторяет цену текста, а где расходится — ошибается она.
_RANK = {"label": 0, "weak": 1, "money": 1, "inline": 1, "text": 2}
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


def _other_deal(fact: PriceFact, *, rent: bool) -> bool:
    """Метка суммы называет другую сделку: «Стоимость покупки 2 млн» в объявлении об аренде."""
    return (SELL_LABEL_RE if rent else RENT_LABEL_RE).search(fact.raw) is not None


def _same_side(pool: list[PriceFact], rent: bool | None) -> list[PriceFact]:
    """Суммы, которые могут быть ценой объявления этой стороны сделки.

    Аренде нужна помесячная цена, продаже — цена без срока, и суточную цену байка
    с месячной путать нельзя — это разница на порядок. Аренда без помесячной цены
    довольствуется посуточной или понедельной; продаже срок не идёт вовсе: «6 млн/мес»
    в объявлении о продаже дома — это доход от аренды соседнего, а не цена дома.

    Сторона неизвестна (живой Telegram-поиск видит только текст): ``price_vnd`` —
    это разовая или месячная цена, поэтому суточная, недельная и годовая не годятся.
    А если в тексте есть и разовая, и месячная («Bán căn hộ 4,39 tỷ, có HĐ thuê 13
    triệu/tháng»), по тексту не угадать, какая из них цена, — честнее не называть.
    """
    if rent is None:
        group = [fact for fact in pool if fact.period in (None, "month")]
        return [] if {fact.period for fact in group} == {None, "month"} else group
    pool = [fact for fact in pool if not _other_deal(fact, rent=rent)]
    fits = (None, "month") if rent else (None,)
    group = [fact for fact in pool if fact.period in fits]
    return group or (pool if rent else [])


def choose_price(
    facts: list[PriceFact], *, rent: bool | None = None, bounds: tuple[int, int] | None = None
) -> PriceFact | None:
    """Одна цена объявления из найденных сумм.

    Сначала границы правдоподобия (если известны), потом сторона сделки и срок
    (`_same_side`). Потом пометка и наименьшая: каталог цен по этажам и срокам
    сводится к «от 9 млн», остальные уходят в ``up_to``.
    """
    pool = [fact for fact in facts if fact.currency == "VND"] or facts
    pool = [r for fact in pool if (r := _resolved(fact, bounds, rent=bool(rent))) is not None]
    group = _same_side(pool, rent)
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


def _is_rent(deal_type: str | None) -> bool | None:
    """Сдают ли предмет; ``None`` — сторона неизвестна («buy», «wanted», пусто)."""
    return None if deal_type not in {"rent_out", "sell"} else deal_type == "rent_out"


def fits_budget(fact: PriceFact, *, rent: bool | None) -> bool:
    """Влезает ли цена в колонку бюджета: донги, а у аренды — за месяц или без срока.

    Суточная цена байка и сумма в долларах в колонку «донги за месяц» не идут: они
    прошли бы любой месячный бюджет (250 тысяч в сутки против потолка в 3 миллиона).
    """
    return fact.currency == "VND" and (not rent or fact.period in (None, "month"))


def parse_price(
    text: str, *, category: str | None = None, deal_type: str | None = None
) -> PriceFact | None:
    """Цена объявления; категория и сторона сделки уточняют срок и границы."""
    return choose_price(
        parse_prices(text), rent=_is_rent(deal_type), bounds=price_bounds(category, deal_type)
    )


def price_hint(
    text: str, *, category: str | None = None, deal_type: str | None = None
) -> tuple[str, int | None]:
    """Вернуть написанную цену и её значение в VND либо честное ``None``.

    Вход живого Telegram-поиска: ему нужны только донги, потому что `price_vnd`
    по определению донги, и только то, что влезает в колонку бюджета (`fits_budget`).
    Категорию и сторону сделки план поиска знает — пусть передаёт: тогда границы
    категории отсеивают сборы («700 000 донгов в месяц» у квартиры за 17 млн), а срок
    читается по стороне. Без них разбор осторожен: суточную цену не называет вовсе.
    """
    fact = parse_price(text, category=category, deal_type=deal_type)
    if fact is None or not fits_budget(fact, rent=_is_rent(deal_type)):
        return "", None
    return fact.raw, fact.amount
