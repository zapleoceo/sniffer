"""Отчёт по фасетам: сколько подходит и как это распределено (чистый домен)."""

from __future__ import annotations

from typing import Any

from sniffer.domain.facets import OPEN_END, facets_from, nice_up, price_edges
from sniffer.sources.base import RawItem


def item(n: int, *, price: int | None = None, district: str | None = None, **attrs: Any) -> RawItem:
    raw: dict[str, Any] = {"listing_id": n, "attributes": attrs}
    if district is not None:
        raw["district"] = district
    return RawItem(
        source="archive", external_id=str(n), url=f"https://t.me/c/{n}", price_vnd=price, raw=raw
    )


def test_counts_known_values_and_keeps_the_unknown_apart() -> None:
    items = [
        item(1, brand="honda"),
        item(2, brand="honda"),
        item(3, brand="yamaha"),
        item(4),
    ]
    report = facets_from(items)
    brand = report.facets["attributes.brand"]
    assert report.total == 4
    assert [(v.value, v.count) for v in brand.values] == [("honda", 2), ("yamaha", 1)]
    assert brand.unknown == 1
    assert brand.known + brand.unknown == report.total


def test_every_facet_adds_up_to_the_total() -> None:
    items = [
        item(i, price=1_000_000 * (i + 1), brand="honda" if i % 2 else None, rooms=i % 3 or None)
        for i in range(30)
    ]
    report = facets_from(items)
    for name, facet in report.facets.items():
        assert facet.known + facet.unknown == report.total, name


def test_buckets_district_area_floor_and_year_from_the_enriched_card() -> None:
    items = [
        item(1, district="my khe", area_m2=45, floor=3, year=2019, engine_cc=125),
        item(2, district="my khe", area_m2=95, floor=3, year=2021, engine_cc=350),
        item(3, area_m2=25, engine_cc=110),
    ]
    facets = facets_from(items).facets
    assert [(v.value, v.count) for v in facets["district"].values] == [("my khe", 2)]
    assert facets["district"].unknown == 1
    assert {v.value for v in facets["attributes.area_m2"].values} == {"30-50", "80+", "до 30"}
    assert {v.value for v in facets["attributes.engine_cc"].values} == {"111-135", "300+", "до 110"}
    assert facets["attributes.floor"].values[0].value == "3"
    assert facets["attributes.year"].known == 2


def test_nonsense_numbers_are_unknown_not_buckets() -> None:
    items = [item(1, area_m2=0, year=1500, floor=-2, rooms=True), item(2, area_m2="много")]
    facets = facets_from(items).facets
    for name in ("attributes.area_m2", "attributes.year", "attributes.floor", "attributes.rooms"):
        assert facets[name].known == 0, name


def test_price_buckets_split_known_prices_and_count_the_missing_ones() -> None:
    items = [item(i, price=(i + 1) * 1_000_000) for i in range(12)] + [item(99)]
    price = facets_from(items).facets["budget.max"]
    assert price.unknown == 1
    assert price.known == 12
    edges = [v.value for v in price.values]
    assert edges[-1] == OPEN_END
    assert [int(e) for e in edges[:-1]] == sorted(int(e) for e in edges[:-1])


def test_price_edges_are_nice_round_numbers() -> None:
    assert nice_up(14_380_000) == 15_000_000
    assert nice_up(950_000) == 950_000
    assert nice_up(0) == 0
    assert price_edges([5, 6]) == ()
    assert all(e % 1_000_000 == 0 for e in price_edges([m * 1_100_000 for m in range(1, 31)]))


def test_an_empty_selection_is_an_empty_report() -> None:
    report = facets_from([])
    assert report.total == 0
    assert all(f.known == 0 and f.unknown == 0 for f in report.facets.values())


def test_an_edge_that_swallows_every_price_is_not_a_boundary() -> None:
    # Округление вверх дало бы границу выше самой дорогой цены: кнопка «до 13 млн»
    # включала бы всё и ничего не сужала.
    prices = [1_000_000] * 3 + [12_100_000] * 3 + [12_200_000] * 3
    assert price_edges(prices) == ()
