"""Цена в проходе догона: политика замены накопленного значения.

Новый разбор цены (`domain/prices.py`) работает для новых сообщений, а в базе
лежат десятки тысяч карточек, посчитанных прежним: у 56% жилья в аренду цены
нет, хотя в тексте она есть, а у части стоят неверные значения («5500» вместо
«5,5 млн»). Проход пересчитывает их из исходного текста, и главный вопрос не
«какая цена верная» — это решает разбор, — а «что можно переписать, не испортив
выбранное осознанно». Ответ — таблица ниже: один исход на карточку, и она же —
единственное место, где политика записана (`judge`).

| было в базе | нашли по тексту | исход | пишем |
|---|---|---|---|
| нет | сумма в донгах | `filled` | колонки цены |
| нет | суточная или в валюте | `rate_only` | атрибуты `rate_*` |
| нет | ничего | `absent` | — |
| есть | то же | `same` | — |
| вне границ категории | иное, подтверждено меткой, внутри границ | `replaced` | колонки цены |
| вне границ | иное, подтверждение слабее метки (или снова вне границ) | `kept_implausible` | — |
| в границах | иное | `disagreed` | — |
| есть | ничего в колонку | `lost` / `lost_implausible` | — |

**Не стираем никогда.** «Потеряно» — значит в тексте ничего не нашли, а в базе
что-то есть: это может быть наша ошибка так же, как и старая, поэтому значение
остаётся и считается в отчёте. Отдельно считается «потеряно, но вне границ»
(5 млрд за аренду дома): оно не исправлено, и владелец должен это видеть.

**Расхождение не правим.** Две правдоподобные цены разошлись — это чаще всего
выбор наименьшей цены каталога вместо первой, и он мог быть осознанным.
Метка здесь ничего не меняет: правило «метка заменяет» касается лишь значений
вне границ, где старое заведомо не цена.

**Суточное и доллары — в атрибуты**, колонка `price_amount` не трогается: 250
тысяч в сутки в месячной колонке прошли бы любой бюджет.

**Атрибуты цены следуют за колонкой.** `price_up_to` пишется, когда колонка
после патча равна новой цене (заполнена, заменена или уже совпала); рядом со
старой, оставленной ценой верх вилки новой дал бы «7 млн — до 11 млн» из двух
разных цен. `rate_*` пишутся всегда: колонке они не противоречат.

**В патч попадает только разница.** Совпавшее не пишется, поэтому повторный
проход пуст и в базу не ходит: идемпотентность держится на этом, а не на
счастливом совпадении.
"""

from __future__ import annotations

from decimal import Decimal

from sniffer.domain.listing_patch import ListingPatch
from sniffer.domain.price_bounds import price_bounds
from sniffer.domain.prices import parse_price
from sniffer.domain.records import Listing
from sniffer.pipeline.listing_price import PriceColumns, price_columns

NAME = "price"

FILLED = "price.filled"
REPLACED = "price.replaced"
SAME = "price.same"
DISAGREED = "price.disagreed"
KEPT_IMPLAUSIBLE = "price.kept_implausible"
LOST = "price.lost"
LOST_IMPLAUSIBLE = "price.lost_implausible"
RATE_ONLY = "price.rate_only"
ABSENT = "price.absent"

# Исходы, при которых колонки цены пишутся; во всех остальных они не трогаются.
_WRITES_COLUMNS = frozenset({FILLED, REPLACED})
# Исходы, после которых колонка равна новой цене: только им положен верх вилки.
_COLUMN_MATCHES = frozenset({FILLED, REPLACED, SAME})


def _outside(amount: Decimal, bounds: tuple[int, int] | None) -> bool:
    """Вне границ здравого смысла; без границ «вне» не определено — значит нет."""
    return bounds is not None and not bounds[0] <= amount <= bounds[1]


def judge(
    old: Decimal | None,
    new: PriceColumns,
    *,
    source: str | None,
    bounds: tuple[int, int] | None,
) -> str:
    """Исход для карточки — политика замены целиком, по таблице модуля.

    `source` — чем подтверждена новая сумма (`label` сильнее всего).
    `bounds` — границы категории и стороны сделки либо `None`, если пара
    неизвестна: тогда старое значение не бывает «вне границ» и не заменяется.
    """
    if new.amount is None:
        if old is None:
            return RATE_ONLY if new.attributes else ABSENT
        return LOST_IMPLAUSIBLE if _outside(old, bounds) else LOST
    if old is None:
        return FILLED
    if old == new.amount:
        return SAME
    if not _outside(old, bounds):
        return DISAGREED
    trusted = source == "label" and not _outside(new.amount, bounds)
    return REPLACED if trusted else KEPT_IMPLAUSIBLE


def _columns(listing: Listing, new: PriceColumns, outcome: str) -> dict[str, object]:
    if outcome not in _WRITES_COLUMNS:
        return {}
    wanted = {
        "price_amount": new.amount,
        "price_currency": new.currency,
        "price_period": new.period,
    }
    return {name: value for name, value in wanted.items() if getattr(listing, name) != value}


def _attributes(listing: Listing, new: PriceColumns, outcome: str) -> dict[str, object]:
    if new.amount is not None and outcome not in _COLUMN_MATCHES:
        return {}
    return {
        key: value
        for key, value in new.attributes.items()
        if key not in listing.attributes or listing.attributes[key] != value
    }


def derive_price(listing: Listing, text: str) -> ListingPatch:
    """Патч цены по исходному тексту; разбор — тот же, что у новых сообщений."""
    fact = parse_price(text, category=listing.category, deal_type=listing.deal_type)
    new = price_columns(fact, listing.deal_type)
    outcome = judge(
        listing.price_amount,
        new,
        source=fact.source if fact is not None else None,
        bounds=price_bounds(listing.category, listing.deal_type),
    )
    return ListingPatch(
        _columns(listing, new, outcome), _attributes(listing, new, outcome), (outcome,)
    )


class PriceDerivation:
    """Вывод «цена» для реестра `pipeline/enrich.py`."""

    name = NAME

    def derive(self, listing: Listing, text: str) -> ListingPatch:
        return derive_price(listing, text)
