"""Fixed verified catalogue used by the request-scoped dialogue simulation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sniffer.sources.base import RawItem


@dataclass(frozen=True, slots=True)
class CatalogLot:
    city: str
    category: str
    deal_type: str
    item: RawItem


def _lot(
    external_id: str,
    title: str,
    price_vnd: int,
    *,
    city: str,
    category: str,
    deal_type: str,
) -> CatalogLot:
    facts = {"city": city, "category": category, "deal_type": deal_type}
    return CatalogLot(
        city,
        category,
        deal_type,
        RawItem(
            source="verified_catalog",
            external_id=external_id,
            url=f"https://catalog.example/{external_id}",
            title=title,
            text=title,
            price_vnd=price_vnd,
            posted_at=datetime(2026, 9, 6, 12, tzinfo=UTC),
            raw={"verified": True, "facts": facts},
        ),
    )


CATALOG_LOTS: tuple[CatalogLot, ...] = (
    _lot(
        "nt-lead",
        "Honda Lead 125 automatic scooter",
        11_000_000,
        city="nha_trang",
        category="motorbike",
        deal_type="sell",
    ),
    _lot(
        "nt-nouvo",
        "Yamaha Nouvo automatic scooter",
        9_000_000,
        city="nha_trang",
        category="motorbike",
        deal_type="sell",
    ),
    _lot(
        "nt-vision-budget",
        "Honda Vision automatic scooter",
        8_500_000,
        city="nha_trang",
        category="motorbike",
        deal_type="sell",
    ),
    _lot(
        "dn-yamaha-budget",
        "Yamaha Exciter manual motorbike 150cc",
        15_000_000,
        city="da_nang",
        category="motorbike",
        deal_type="sell",
    ),
    _lot(
        "dn-yamaha-pricey",
        "Yamaha Exciter manual motorbike 155cc",
        23_000_000,
        city="da_nang",
        category="motorbike",
        deal_type="sell",
    ),
    _lot(
        "dn-vision",
        "Honda Vision automatic scooter",
        10_000_000,
        city="da_nang",
        category="motorbike",
        deal_type="sell",
    ),
    _lot(
        "nt-apartment",
        "Furnished studio apartment for rent",
        9_000_000,
        city="nha_trang",
        category="apartment",
        deal_type="rent_out",
    ),
    _lot(
        "nt-apartment-two",
        "Furnished 2 bedroom apartment for rent",
        10_000_000,
        city="nha_trang",
        category="apartment",
        deal_type="rent_out",
    ),
    _lot(
        "dn-room",
        "Room for rent in Da Nang",
        8_000_000,
        city="da_nang",
        category="room",
        deal_type="rent_out",
    ),
    _lot(
        "nt-wanted-vision",
        "Куплю Honda Vision automatic scooter",
        10_000_000,
        city="nha_trang",
        category="motorbike",
        deal_type="wanted",
    ),
    _lot(
        "nt-wanted-apartment",
        "Сниму меблированную квартиру в Нячанге",
        9_000_000,
        city="nha_trang",
        category="apartment",
        deal_type="wanted",
    ),
)
