"""Объясняется ли правка цены сменой стороны или категории после вердикта модели.

Цена карточки читается один раз, при создании, а вердикт модели
(`worker/screening.py`) приходит позже и может сменить сторону сделки и
категорию: границы правдоподобия у аренды и продажи различаются на два порядка,
и «аренда квартиры» выбрасывает 1,36 млрд, которые для продажи — цена. Проход
догона читает цену под ИТОГОВОЙ парой, а отчёт должен сказать владельцу, сколько
записей объясняется именно сменой пары.

**Объясняется — значит воспроизводится.** Правка объяснена сменой, если чтение
текста под ПРЕЖНЕЙ парой (той, что воронка присвоила бы при создании) даёт ровно
то, что лежит в карточке, а под итоговой — иное. Одного различия пар мало: у 6%
карточек пара отличается, но почти везде цену нашёл бы и прежний разбор, и дело
не в стороне, а в разборе («5500» вместо «5,5 млн» от стороны не зависит).

Прежнюю пару даёт та же логика, что у воронки (`worker/archive.py`): категория —
первый предмет, названный в тексте (`category_hints`), сторона — глагол сделки
из `parse_query` либо умолчание категории (`offer_deal_type`). Своей копии правил
здесь нет: расходятся они — расходится и смысл отчёта, поэтому берём их там, где
они живут. Знание о `search` вносит этот модуль процесса, а не воронка
(`pipeline` импортировать `search` не вправе, `tests/test_layers.py`).
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from sniffer.domain.passport import Category
from sniffer.domain.records import Listing
from sniffer.pipeline.archive import offer_deal_type
from sniffer.pipeline.enrich_price import read_amount
from sniffer.search.intake_rules import parse_query
from sniffer.search.vocabulary import category_hints


def funnel_view(listing: Listing, text: str) -> tuple[str, str]:
    """(категория, сторона), как их присвоила бы воронка этому тексту."""
    hints = category_hints(text)
    if hints:
        category: Category | None = hints[0]
    else:
        try:
            category = Category(listing.category)
        except ValueError:
            category = None
    intent = parse_query(text, default_city=listing.city).intent
    return (category.value if category else listing.category), offer_deal_type(intent, category)


def changed_by_verdict(
    listing: Listing,
    text: str,
    *,
    funnel: Callable[[Listing, str], tuple[str, str]] = funnel_view,
    read: Callable[..., Decimal | None] = read_amount,
) -> bool:
    """Объясняется ли правка цены этой карточки сменой пары после создания.

    Пара не менялась — нечего объяснять. Менялась — смотрим, что дало бы чтение
    текста под прежней парой: совпало с тем, что лежит в карточке, значит цену
    испортила смена, а не прежний разбор.
    """
    origin = funnel(listing, text)
    if origin == (listing.category, listing.deal_type):
        return False
    return read(text, *origin) == listing.price_amount
