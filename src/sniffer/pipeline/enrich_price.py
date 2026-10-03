"""Цена в проходе догона: политика замены накопленного значения.

Новый разбор цены (`domain/prices.py`) работает для новых сообщений, а в базе
лежат десятки тысяч карточек, посчитанных прежним: у 56% жилья в аренду цены
нет, хотя в тексте она есть, а у части стоят неверные значения («5500» вместо
«5,5 млн»). Проход пересчитывает их из исходного текста, и главный вопрос не
«какая цена верная» — это решает разбор, — а «что можно переписать, не испортив
выбранное осознанно». Ответ — таблица ниже: один исход на карточку, и она же —
единственное место, где политика записана (`judge`).

**Цена читается под ИТОГОВОЙ стороной и категорией карточки.** Вердикт модели
(`worker/screening.py`) приходит после извлечения и может сменить сторону
сделки и категорию — а границы правдоподобия у аренды и продажи различаются на
два порядка. Замер ревью 03.10.2026 на 18 888 карточках: у 5,6% сторона или
категория после вердикта не та, под которой читалась цена, и у 92 цена из-за
этого неверна. Поэтому разбор и границы берут `category` и `deal_type` СТРОКИ
такими, какие они сейчас; проход их не пересчитывает и не пишет.

| было в базе | нашли по тексту | исход | пишем |
|---|---|---|---|
| цены нет | сумма в донгах | `filled` | колонки цены |
| цены нет | суточная или в валюте | `rate_only` | атрибуты `rate_*` |
| цены нет | ничего | `absent` | — |
| есть | то же | `same` | — |
| вне границ итоговой стороны | сумма внутри границ | `replaced` | колонки цены |
| вне границ | ничего или только ставка | `erased` | колонки цены → пусто |
| в границах | иная сумма | `disagreed` | — |
| в границах | ничего или только ставка | `lost` | — |

**Вне границ — заведомая ошибка, и стирать её можно.** 5 млрд за аренду дома,
суточная ставка в месячной колонке, «13 000 000 разово» у квартиры, которую
купили бы за 4,39 млрд: это не цена, и честная замена ей — найденная цена или
пустое место. Заменяет любая правдоподобная новая сумма, какой бы строкой
текста она ни подтверждалась: старое значение хуже любого правдоподобного.

**В границах не стираем и не правим.** «Потеряно» значит «в тексте цены не
нашли, а в базе правдоподобная есть»: ошибиться мог и прежний разбор, и новый,
поэтому значение остаётся и считается в отчёте. Две правдоподобные цены
разошлись — чаще всего это наименьшая цена каталога вместо первой, и выбор мог
быть осознанным: тоже остаётся и считается.

**Суточное и доллары — в атрибуты**, колонка `price_amount` их не принимает: 250
тысяч в сутки в месячной колонке прошли бы любой бюджет.

**Атрибуты цены пересобираются из нового факта целиком** — семья
`PRICE_ATTRIBUTES` (`price_up_to`, `rate_*`): что новый разбор выдал, то
пишется, а что не выдал, то убирается, иначе от прежней стороны сделки
оставались бы чужие `rate_*`. Остальные атрибуты карточки (марка, комнаты, всё,
что извлечено иначе) не трогаются. Одно исключение — `price_up_to`: верх вилки
принадлежит колонке. Рядом с оставленной старой ценой (`disagreed`, `lost`) верх
вилки новой дал бы «7 млн — до 11 млн» из двух разных цен, а уже стоящий — от
старой цены, которую мы оставили, — не стирается. Ставки `rate_*` от колонки не
зависят и пересобираются всегда.

**В патч попадает только разница**, поэтому повторный проход пуст и в базу не
ходит: идемпотентность держится на этом, а не на счастливом совпадении.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from sniffer.domain.listing_patch import ListingPatch
from sniffer.domain.price_bounds import price_bounds
from sniffer.domain.prices import PriceFact, parse_price
from sniffer.domain.records import Listing
from sniffer.pipeline.listing_price import PriceColumns, price_columns

NAME = "price"

FILLED = "price.filled"
REPLACED = "price.replaced"
ERASED = "price.erased"
SAME = "price.same"
DISAGREED = "price.disagreed"
LOST = "price.lost"
RATE_ONLY = "price.rate_only"
ABSENT = "price.absent"

# Ключи атрибутов, которые пишет `price_columns` и которыми поэтому вправе
# распоряжаться этот вывод; тест сверяет список с тем, что `price_columns`
# способна записать, — новый ключ там без записи здесь краснит тест.
PRICE_ATTRIBUTES = frozenset(
    {"price_up_to", "rate_amount", "rate_currency", "rate_per", "rate_up_to"}
)
# Верх вилки принадлежит колонке цены, а не тексту сам по себе (см. docstring).
_COLUMN_BOUND = frozenset({"price_up_to"})

# Исходы, при которых колонки цены пишутся; во всех остальных они не трогаются.
_FILLS_COLUMNS = frozenset({FILLED, REPLACED})
# Исходы, при которых колонка остаётся с ПРЕЖНИМ значением, не равным новому.
_KEEPS_OLD_PRICE = frozenset({DISAGREED, LOST})

ParsePrice = Callable[..., PriceFact | None]
# Границы правдоподобия по паре «категория, сторона»; внедряются так же, как разбор.
BoundsOf = Callable[[str | None, str | None], tuple[int, int] | None]


def _outside(amount: Decimal, bounds: tuple[int, int] | None) -> bool:
    """Вне границ здравого смысла; без границ «вне» не определено — значит нет."""
    return bounds is not None and not bounds[0] <= amount <= bounds[1]


def judge(old: Decimal | None, new: PriceColumns, *, bounds: tuple[int, int] | None) -> str:
    """Исход для карточки — политика замены целиком, по таблице модуля.

    `bounds` — границы итоговой категории и стороны либо `None`, если пара
    неизвестна: тогда старое значение не бывает «вне границ», не заменяется и
    не стирается.
    """
    if old is None:
        if new.amount is not None:
            return FILLED
        return RATE_ONLY if new.attributes else ABSENT
    if new.amount is not None and old == new.amount:
        return SAME
    if _outside(old, bounds):
        if new.amount is None:
            return ERASED
        return DISAGREED if _outside(new.amount, bounds) else REPLACED
    return LOST if new.amount is None else DISAGREED


def _columns(listing: Listing, new: PriceColumns, outcome: str) -> dict[str, object]:
    if outcome in _FILLS_COLUMNS:
        wanted: dict[str, object] = {
            "price_amount": new.amount,
            "price_currency": new.currency,
            "price_period": new.period,
        }
    elif outcome == ERASED:
        wanted = {"price_amount": None, "price_currency": None, "price_period": None}
    else:
        return {}
    return {name: value for name, value in wanted.items() if getattr(listing, name) != value}


def _attributes(
    listing: Listing, new: PriceColumns, outcome: str
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Что записать и что убрать из атрибутов: семья цены — из нового факта целиком."""
    managed = PRICE_ATTRIBUTES
    wanted = dict(new.attributes)
    if outcome in _KEEPS_OLD_PRICE:
        managed = managed - _COLUMN_BOUND
        wanted = {key: value for key, value in wanted.items() if key not in _COLUMN_BOUND}
    current = listing.attributes
    write = {k: v for k, v in wanted.items() if k not in current or current[k] != v}
    remove = tuple(sorted(k for k in managed if k in current and k not in wanted))
    return write, remove


def read_amount(
    text: str, category: str, deal_type: str, *, parse: ParsePrice = parse_price
) -> Decimal | None:
    """Сумма, которую получила бы колонка цены, если читать текст под этой парой.

    Одна и та же строка читается по-разному под разными парами «категория,
    сторона» — границы правдоподобия у аренды и продажи различаются на два порядка.
    Сравнив чтение под прежней парой с тем, что лежит в карточке, можно сказать,
    испортила ли цену смена пары или прежний разбор.
    """
    return price_columns(parse(text, category=category, deal_type=deal_type), deal_type).amount


def derive_price(
    listing: Listing,
    text: str,
    *,
    parse: ParsePrice = parse_price,
    bounds_of: BoundsOf = price_bounds,
) -> ListingPatch:
    """Патч цены по исходному тексту; разбор и границы — те же, что у новых сообщений.

    `parse` и `bounds_of` подменяем в тестах: политика проверяется на заданных
    фактах и заданных границах, и правки самого разбора или таблицы границ её
    тесты не ломают.
    """
    fact = parse(text, category=listing.category, deal_type=listing.deal_type)
    new = price_columns(fact, listing.deal_type)
    outcome = judge(
        listing.price_amount, new, bounds=bounds_of(listing.category, listing.deal_type)
    )
    write, remove = _attributes(listing, new, outcome)
    return ListingPatch(_columns(listing, new, outcome), write, (outcome,), remove)


class PriceDerivation:
    """Вывод «цена» для реестра `pipeline/enrich.py`."""

    name = NAME

    def __init__(self, parse: ParsePrice = parse_price, bounds_of: BoundsOf = price_bounds) -> None:
        self._parse, self._bounds_of = parse, bounds_of

    def derive(self, listing: Listing, text: str) -> ListingPatch:
        return derive_price(listing, text, parse=self._parse, bounds_of=self._bounds_of)
