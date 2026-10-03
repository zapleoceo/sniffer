"""Цена карточки: что из найденной в тексте суммы идёт в колонки, а что в атрибуты.

Колонка `price_amount` — донги, а `price_period` — «month» или «once»: именно так
её читают отбор по бюджету, ранжирование и карточка. Всё, что в этот договор не
влезает, — суточная цена байка, сумма в долларах, верхняя граница вилки, — не
выбрасывается и не прячется в колонку под чужим смыслом: оно лежит в атрибутах,
и карточка может показать его честно («от 250 тыс. ₫/сутки»), а бюджетный фильтр
считает такую цену неизвестной, как и раньше.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sniffer.domain.prices import PriceFact, fits_budget


@dataclass(frozen=True, slots=True)
class PriceColumns:
    """Значения для колонок цены и дополнительные атрибуты карточки."""

    amount: Decimal | None = None
    currency: str | None = None
    period: str | None = None
    attributes: dict[str, object] = field(default_factory=dict)


def price_columns(fact: PriceFact | None, deal_type: str) -> PriceColumns:
    """Разложить найденную цену по колонкам и атрибутам.

    Срок цены следует за стороной сделки: сдают помесячно, продают разово
    (так же `listings.apply_screen` пересчитывает его после вердикта модели).
    """
    if fact is None:
        return PriceColumns()
    rent = deal_type == "rent_out"
    if fits_budget(fact, rent=rent):
        extra: dict[str, object] = {"price_up_to": fact.up_to} if fact.up_to else {}
        return PriceColumns(Decimal(fact.amount), "VND", "month" if rent else "once", extra)
    kept: dict[str, object] = {
        "rate_amount": fact.amount,
        "rate_currency": fact.currency,
        "rate_per": fact.period or ("month" if rent else "once"),
    }
    if fact.up_to:
        kept["rate_up_to"] = fact.up_to
    return PriceColumns(attributes=kept)
