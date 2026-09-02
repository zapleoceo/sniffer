"""Проверенные live-находки становятся честными карточками общего каталога."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sniffer.domain.passport import Category, Intent, Passport
from sniffer.domain.records import Listing
from sniffer.sources.base import RawItem
from sniffer.sources.catalog_sink import remember


def passport(**overrides: object) -> Passport:
    values: dict[str, object] = {
        "intent": Intent.BUY,
        "category": Category.MOTORBIKE,
        "city": "nha_trang",
        "attributes": {"brand": "honda"},
    }
    values.update(overrides)
    return Passport(**values)  # type: ignore[arg-type]


def item(*, posted: bool = True) -> RawItem:
    return RawItem(
        source="chotot",
        external_id="42",
        url="https://example.test/42",
        title="Honda Lead",
        text="Автомат",
        price_vnd=12_000_000,
        posted_at=datetime(2026, 9, 2, tzinfo=UTC) if posted else None,
    )


async def test_a_verified_live_item_is_stored_without_invented_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Listing] = []

    async def store(rows: list[Listing]) -> int:
        captured.extend(rows)
        return len(rows)

    monkeypatch.setattr("sniffer.sources.chat_directory.store_listings", store)

    assert await remember([item()], passport()) == 1
    listing = captured[0]
    assert listing.source == "chotot"
    assert listing.external_id == "42"
    assert listing.deal_type == "sell"
    assert listing.attributes == {}


async def test_undated_or_unscoped_items_are_not_cached() -> None:
    assert await remember([item(posted=False)], passport()) == 0
    assert await remember([item()], passport(city=None)) == 0
    assert await remember([item()], passport(category=None)) == 0


async def test_cache_failure_never_breaks_the_current_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(_rows: list[object]) -> int:
        raise RuntimeError("postgres unavailable")

    monkeypatch.setattr("sniffer.sources.chat_directory.store_listings", broken)
    assert await remember([item()], passport()) == 0
