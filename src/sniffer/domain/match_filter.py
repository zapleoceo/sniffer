"""Условия отбора карточек по паспорту: ОДНО знание для диалога и для слежения.

Диалог (`sources/chat_directory.search_listings`) и монитор (`matching.filter_for`) раньше
собирали `MatchFilter` каждый по-своему: монитор не знал ни умолчания «байк без слова
электро — бензиновый», ни полосы объёма, и электробайк уходил подписчику ДВС, а «200
кубиков» не пропускала ни одной карточки с известным объёмом (D3, docs/architecture.md §2:
расхождение критериев двух путей — дефект). Здесь решение принимается один раз; оба пути
отличаются только окном свежести и способом получить потолок цены в донгах.

Бюджет в EUR и RUB потолка не даёт НИГДЕ: курса для них нет ни у диалога, ни у слежения,
и честнее не сужать, чем сужать по выдуманному числу (`ceiling_vnd`).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sniffer.domain.passport import (
    Budget,
    Currency,
    Intent,
    counterpart_deal_type,
    engine_cc_bounds,
    with_default_attributes,
)
from sniffer.domain.records import MatchFilter

# Свойства, у которых известное значение обязано совпасть, а неизвестное не мешает.
# Модель и объём идут своими полями `MatchFilter`: у них другая семантика (см. там).
EXACT_ATTRIBUTES = ("brand", "transmission", "rooms", "power")


def ceiling_vnd(budget: Budget, usd_vnd: float | None) -> Decimal | None:
    """Потолок цены в донгах или `None`, если честно сузить нечем.

    Донги — как есть, доллары — по курсу. Валюта известна, а курса нет (USD без курса,
    EUR, RUB): не сужаем, потому что выдуманный курс занизил бы бюджет и спрятал нужное.
    """
    if budget.max is None:
        return None
    if budget.currency is Currency.VND:
        return Decimal(str(budget.max))
    if budget.currency is Currency.USD and usd_vnd is not None:
        return Decimal(str(budget.max * usd_vnd))
    return None


def build_match_filter(
    *,
    city: str,
    category: str | None,
    intent: Intent | None,
    ceiling: Decimal | None,
    since: datetime,
    attributes: dict[str, Any],
) -> MatchFilter:
    """`MatchFilter` по сторонам паспорта: умолчания категории, полоса объёма, модель."""
    merged = with_default_attributes(category, attributes)
    low, high = engine_cc_bounds(merged.get("engine_cc"), merged.get("engine_cc_dir"))
    return MatchFilter(
        city=city,
        category=category,
        # Паспорт описывает сторону клиента, карточка — сторону автора объявления:
        # покупателю нужен продавец, арендатору — арендодатель.
        deal_type=counterpart_deal_type(intent),
        max_price_vnd=ceiling,
        since=since,
        attributes={
            key: merged[key] for key in EXACT_ATTRIBUTES if merged.get(key) not in (None, "")
        },
        model=str(merged.get("model") or "").strip() or None,
        engine_cc_min=low,
        engine_cc_max=high,
    )
