"""Собственный каталог участвует в том же контракте, что внешние источники."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sniffer.domain.records import Listing
from sniffer.sources.archive import ArchiveSource


@dataclass
class Catalog:
    rows: list[Listing] = field(default_factory=list)
    params: dict[str, object] = field(default_factory=dict)

    async def search(self, params: dict[str, object], *, limit: int) -> list[Listing]:
        self.params = params
        assert limit == 100
        return self.rows


def listing() -> Listing:
    return Listing(
        raw_message_id=None,
        source="chotot",
        external_id="ad-42",
        deal_type="sell",
        category="motorbike",
        city="nha_trang",
        title="Honda Lead",
        summary="Автомат, документы есть",
        tg_link="https://example.test/ad-42",
        price_amount=Decimal("12000000"),
        price_currency="VND",
        posted_at=datetime(2026, 9, 2, tzinfo=UTC),
        id=7,
    )


async def test_archive_returns_the_original_source_identity() -> None:
    catalog = Catalog(rows=[listing()])
    source = ArchiveSource(catalog)
    (item,) = await source.search("ignored", {"city": "nha_trang"})

    assert (item.source, item.external_id) == ("chotot", "ad-42")
    assert item.price_vnd == 12_000_000
    assert catalog.params == {"city": "nha_trang"}


async def test_archive_failure_degrades_without_breaking_other_sources() -> None:
    class Broken(Catalog):
        async def search(self, params: dict[str, object], *, limit: int) -> list[Listing]:
            raise RuntimeError("database unavailable")

    source = ArchiveSource(Broken())
    assert await source.search("bike", {"city": "nha_trang"}) == []
    assert source.degraded
