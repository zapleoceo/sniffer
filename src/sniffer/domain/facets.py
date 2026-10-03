"""Что лежит в базе под этот запрос: отчёт по фасетам.

Чистый домен, без ввода-вывода: на вход — уже найденные и отобранные карточки,
на выход — сколько их и как они распределены по полям. По этому отчёту
планировщик (`clarify.py`) решает, о чём спросить, а выдача называет честное
число. Отчёт строится из ТЕХ ЖЕ карточек, что идут в показ, а не отдельным
запросом в базу: два запроса с разным окном свежести и разным дедупом давали бы
«Нашёл 37», а показано 31 — число, которому нельзя верить.

Правило пропусков одно, «неизвестное остаётся»: карточка, не назвавшая поле,
не противоречит ответу (так отбирает и `search.relevance`), поэтому в фасете
она живёт отдельным счётчиком `unknown`, а не выбрасывается.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

TARGET_SIZE = 10

# Верхняя граница «больше таких нет» для открытых корзин и цены.
OPEN_END = "max"

_ENGINE_EDGES: tuple[tuple[int, str], ...] = (
    (110, "до 110"),
    (135, "111-135"),
    (175, "136-175"),
    (300, "176-300"),
)
_AREA_EDGES: tuple[tuple[int, str], ...] = ((30, "до 30"), (50, "30-50"), (80, "50-80"))


class Facetable(Protocol):
    """То, что нужно фасету от строки выдачи (подходит `sources.base.RawItem`)."""

    @property
    def price_vnd(self) -> int | None: ...

    @property
    def raw(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class FacetValue:
    value: str
    count: int


@dataclass(frozen=True, slots=True)
class Facet:
    """Распределение одного поля. `unknown` — карточки, где поле не названо."""

    field: str
    values: tuple[FacetValue, ...]
    unknown: int

    @property
    def known(self) -> int:
        return sum(item.count for item in self.values)


@dataclass(frozen=True, slots=True)
class FacetReport:
    """Сколько подходит и как это распределено. `total` — длина того, что покажут."""

    total: int
    facets: Mapping[str, Facet] = field(default_factory=dict)


def _attributes(item: Facetable) -> Mapping[str, Any]:
    value = item.raw.get("attributes")
    return value if isinstance(value, Mapping) else {}


def _text(key: str) -> Callable[[Facetable], str | None]:
    def read(item: Facetable) -> str | None:
        value = _attributes(item).get(key)
        return str(value).strip().lower() if isinstance(value, str) and value.strip() else None

    return read


def _number(key: str) -> Callable[[Facetable], int | None]:
    def read(item: Facetable) -> int | None:
        value = _attributes(item).get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value) if value > 0 else None

    return read


def _bucket(number: int | None, edges: tuple[tuple[int, str], ...], last: str) -> str | None:
    if number is None:
        return None
    return next((label for edge, label in edges if number <= edge), last)


def _district(item: Facetable) -> str | None:
    value = item.raw.get("district") or _attributes(item).get("district")
    return str(value).strip().lower() if isinstance(value, str) and value.strip() else None


def _furnished(item: Facetable) -> str | None:
    value = _attributes(item).get("furnished")
    return None if not isinstance(value, bool) else ("yes" if value else "no")


def _rooms(item: Facetable) -> str | None:
    value = _number("rooms")(item)
    return None if value is None else str(value)


def _engine(item: Facetable) -> str | None:
    return _bucket(_number("engine_cc")(item), _ENGINE_EDGES, "300+")


def _area(item: Facetable) -> str | None:
    return _bucket(_number("area_m2")(item), _AREA_EDGES, "80+")


def _year(item: Facetable) -> str | None:
    value = _number("year")(item)
    return str(value) if value is not None and 1980 <= value <= 2100 else None


def _floor(item: Facetable) -> str | None:
    value = _number("floor")(item)
    return None if value is None else str(value)


# Поля, которые отчёт считает по умолчанию. Ключ — путь поля в паспорте, где он
# есть: по нему планировщик находит запись реестра `fields.SPECS`. Поля без
# записи реестра (район, площадь, этаж, год) отчёт считает всё равно: это знание
# о базе, и вопрос на них появится одной записью реестра.
EXTRACTORS: dict[str, Callable[[Facetable], str | None]] = {
    "attributes.brand": _text("brand"),
    "attributes.model": _text("model"),
    "attributes.transmission": _text("transmission"),
    "attributes.rooms": _rooms,
    "attributes.furnished": _furnished,
    "attributes.engine_cc": _engine,
    "attributes.year": _year,
    "attributes.area_m2": _area,
    "attributes.floor": _floor,
    "district": _district,
}


def nice_up(value: float) -> int:
    """Округлить вверх до двух значащих цифр: 14 380 000 → 15 000 000, чтобы граница читалась."""
    if value <= 0:
        return 0
    magnitude = 10 ** max(len(str(int(value))) - 2, 0)
    return int(-(-value // magnitude) * magnitude)


def price_edges(prices: Sequence[int]) -> tuple[int, ...]:
    """Верхние границы ценовых корзин: красивые округления трети и двух третей цен."""
    ordered = sorted(prices)
    if len(ordered) < 3:
        return ()
    edges = {nice_up(ordered[len(ordered) // 3]), nice_up(ordered[2 * len(ordered) // 3])}
    return tuple(sorted(edge for edge in edges if edge < ordered[-1]))


def _price_facet(items: Sequence[Facetable]) -> Facet:
    prices = [item.price_vnd for item in items if item.price_vnd]
    edges = price_edges(prices)
    counts: Counter[str] = Counter()
    for price in prices:
        counts[str(next((edge for edge in edges if price <= edge), OPEN_END))] += 1
    values = tuple(
        FacetValue(key, counts[key]) for key in [*map(str, edges), OPEN_END] if counts[key]
    )
    return Facet("budget.max", values, unknown=len(items) - len(prices))


def facets_from(items: Sequence[Facetable]) -> FacetReport:
    """Отчёт по тем карточкам, что идут в показ. Порядок значений — по убыванию числа."""
    facets: dict[str, Facet] = {"budget.max": _price_facet(items)}
    for name, read in EXTRACTORS.items():
        counts = Counter(value for item in items if (value := read(item)) is not None)
        ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
        facets[name] = Facet(
            name,
            tuple(FacetValue(value, count) for value, count in ordered),
            unknown=len(items) - sum(counts.values()),
        )
    return FacetReport(total=len(items), facets=facets)
