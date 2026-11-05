"""The owner's northern-house request stays constrained through both search paths."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.dialects import postgresql

from sniffer.bot.wording import accepted
from sniffer.db import models
from sniffer.db.mappers import passport_values, to_stored_passport
from sniffer.db.repositories.listings import _filtered
from sniffer.domain.districts import PLACE_BY_SLUG
from sniffer.domain.records import Listing, MatchFilter
from sniffer.matching.rules import filter_for, worth_sending
from sniffer.search.intake import QueryIntake
from sniffer.search.intake_rules import parse_query
from sniffer.search.plan import SearchTask, context_params
from sniffer.search.refinements import refine
from sniffer.search.relevance import rank_items
from sniffer.sources import chat_directory
from sniffer.sources.base import RawItem

QUERY = "2 комнаты минимум / Дом / Нячанг / Район север / До 10 млн"
ACTUAL_QUERY_102 = "Дом\nНячанг \nСевер нячанга \nМиримум 2 комнаты \nОт 7 млн до 15 млн в масяц"


def test_original_request_102_confirms_every_hard_condition() -> None:
    passport = parse_query(ACTUAL_QUERY_102)
    assert passport.attributes["rooms"] == 2
    assert passport.attributes["rooms_min"] is True
    assert passport.districts
    assert (passport.budget.min, passport.budget.max) == (7_000_000, 15_000_000)
    line = accepted(passport).splitlines()[0]
    assert "дом" in line
    assert "район: Север Нячанга" in line
    assert "от 7 000 000 до 15 000 000 VND" in line
    assert "не менее 2 комнат" in line
    found = rank_items(
        passport,
        [
            _card(1, "vinh_hai", 1, 9_000_000),
            _card(2, "vinh_hai", 2, 7_000_000),
            _card(3, "vinh_hai", 3, 15_000_000),
            _card(4, "vinh_hai", 3, 6_000_000),
            _card(5, "vinh_hai", 3, 16_000_000),
        ],
        usd_vnd=None,
    )
    assert {item.external_id for item in found} == {"2", "3"}


def test_legacy_saved_range_recovers_floor_on_repeat_without_writing_row() -> None:
    original = parse_query(ACTUAL_QUERY_102)
    row = models.Passport(id=71, user_id=1, version=2, root_id=71, **passport_values(original))
    row.budget = {**row.budget, "min": None}
    saved_budget = dict(row.budget)

    repeated = to_stored_passport(row).passport
    assert row.budget == saved_budget
    assert (repeated.budget.min, repeated.budget.max) == (7_000_000, 15_000_000)
    assert repeated.districts == original.districts
    assert repeated.attributes["rooms_min"] is True
    assert {
        item.external_id
        for item in rank_items(
            repeated,
            [
                _card(1, "vinh_hai", 2, 6_000_000),
                _card(2, "vinh_hai", 2, 7_000_000),
                _card(3, "vinh_hai", 3, 15_000_000),
            ],
            usd_vnd=None,
        )
    } == {"2", "3"}


@pytest.mark.parametrize(
    "raw_query, maximum",
    [
        (ACTUAL_QUERY_102 + "\nLatest follow-up: до 15 млн", 15_000_000),
        (ACTUAL_QUERY_102, 12_000_000),
    ],
)
def test_legacy_floor_does_not_override_changed_budget(raw_query: str, maximum: int) -> None:
    original = parse_query(ACTUAL_QUERY_102)
    row = models.Passport(
        id=72,
        user_id=1,
        version=3,
        root_id=71,
        **passport_values(original.model_copy(update={"raw_query": raw_query})),
    )
    row.budget = {**row.budget, "min": None, "max": maximum}
    assert to_stored_passport(row).passport.budget.min is None


def test_legacy_floor_does_not_override_an_explicit_saved_floor() -> None:
    original = parse_query(ACTUAL_QUERY_102)
    row = models.Passport(id=73, user_id=1, version=3, root_id=71, **passport_values(original))
    row.budget = {**row.budget, "min": 8_000_000}
    assert to_stored_passport(row).passport.budget.min == 8_000_000


def test_confirmation_does_not_claim_a_district_without_grounded_location() -> None:
    passport = parse_query("Дом в Нячанге, не менее 2 комнаты, до 10 млн")
    assert passport.districts == []
    assert "район:" not in accepted(passport)


@pytest.mark.parametrize("query", [QUERY, ACTUAL_QUERY_102])
def test_semantic_intake_keeps_grounded_district_and_minimum_rooms(query: str) -> None:
    class Broker:
        def __init__(self) -> None:
            self.called = False

        async def structured(self, *_args: object, **_kwargs: object) -> dict[str, str]:
            self.called = True
            return {"category": "house", "city": "nha_trang", "budget_max": "10000000"}

    broker = Broker()
    passport = asyncio.run(QueryIntake(broker=broker).parse(query))
    assert broker.called
    assert passport.districts == parse_query(query).districts
    assert passport.attributes["rooms_min"] is True


def _card(number: int, district: str | None, rooms: int, price: int) -> RawItem:
    return RawItem(
        source="archive",
        external_id=str(number),
        url=f"https://example.test/{number}",
        title=f"Дом {rooms} комнаты",
        text=f"Аренда дома, объект номер {number}",
        price_vnd=price,
        raw={"district": district},
    )


def test_northern_house_and_budget_edit_reach_the_plan_and_monitor_filter() -> None:
    original = parse_query(QUERY)
    updated = refine(
        original, parse_query("изменить бюджет на 7–15 млн"), "изменить бюджет на 7–15 млн"
    )

    assert original.attributes["rooms_min"] is True
    assert original.budget.max == 10_000_000
    assert updated.districts == original.districts
    assert updated.attributes["rooms"] == 2
    assert (updated.budget.min, updated.budget.max) == (7_000_000, 15_000_000)

    params = context_params(updated)
    assert params["districts"] == updated.districts
    assert params["attributes"] == {"rooms": 2, "rooms_min": True}
    task = SearchTask(source="archive", query="", params={"districts": ["west"]})
    assert task.with_defaults(params).params["districts"] == updated.districts
    monitor = filter_for(updated, now=datetime(2026, 11, 4, tzinfo=UTC))
    assert monitor is not None
    assert monitor.districts == tuple(updated.districts)
    assert monitor.zone == "north"
    assert monitor.rooms_min == 2
    assert (monitor.min_price_vnd, monitor.max_price_vnd) == (
        Decimal(7_000_000),
        Decimal(15_000_000),
    )

    statement = _filtered(monitor).compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    sql = str(statement)
    assert "district IN" in sql
    assert "north" in statement.params.values()
    assert "rooms" in statement.params.values()
    assert "price_amount >=" in sql


def test_final_cards_require_north_and_at_least_two_rooms() -> None:
    wanted = parse_query(QUERY)
    north = "vinh_hai"
    west = next(
        place.slug
        for place in PLACE_BY_SLUG.values()
        if place.city == "nha_trang" and place.zone == "west"
    )
    found = rank_items(
        wanted,
        [
            _card(1, north, 2, 9_000_000),
            _card(2, north, 3, 9_000_000),
            _card(3, north, 1, 9_000_000),
            _card(4, west, 3, 9_000_000),
            _card(5, None, 3, 9_000_000),
        ],
        usd_vnd=None,
    )
    assert {item.external_id for item in found} == {"1", "2"}


def test_budget_range_excludes_known_prices_outside_it() -> None:
    wanted = refine(
        parse_query(QUERY),
        parse_query("изменить бюджет на 7–15 млн"),
        "изменить бюджет на 7–15 млн",
    )
    found = rank_items(
        wanted,
        [
            _card(1, "vinh_hai", 2, 6_000_000),
            _card(2, "vinh_hai", 2, 7_000_000),
            _card(3, "vinh_hai", 3, 15_000_000),
            _card(4, "vinh_hai", 3, 16_000_000),
        ],
        usd_vnd=None,
    )
    assert {item.external_id for item in found} == {"2", "3"}


def test_explicit_room_edit_replaces_the_previous_minimum() -> None:
    revised = refine(
        parse_query(QUERY), parse_query("в доме ровно 3 комнаты"), "в доме ровно 3 комнаты"
    )
    assert revised.attributes["rooms"] == 3
    assert "rooms_min" not in revised.attributes


async def test_catalog_source_receives_location_and_room_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[MatchFilter] = []

    class Repository:
        def __init__(self, session: object) -> None:
            pass

        async def search_catalog(self, spec: MatchFilter, *, limit: int) -> list[Listing]:
            captured.append(spec)
            return []

    @asynccontextmanager
    async def sessions() -> AsyncIterator[object]:
        yield object()

    monkeypatch.setattr("sniffer.db.ListingRepository", Repository)
    monkeypatch.setattr("sniffer.db.session_scope", sessions)
    params = context_params(parse_query(QUERY))
    assert await chat_directory.search_listings(params, limit=100) == []
    assert len(captured) == 1
    assert captured[0].zone == "north"
    assert captured[0].rooms_min == 2
    assert captured[0].districts == tuple(params["districts"])


def test_zone_only_location_is_known_but_unlocated_listing_is_not_north() -> None:
    wanted = parse_query(QUERY)
    zone_only = _card(11, None, 2, 9_000_000)
    zone_only.raw["attributes"] = {"zone": "north"}
    unknown = _card(12, None, 2, 9_000_000)
    west = _card(13, None, 2, 9_000_000)
    west.raw["attributes"] = {"zone": "west"}
    assert {
        item.external_id for item in rank_items(wanted, [zone_only, unknown, west], usd_vnd=None)
    } == {"11"}

    now = datetime(2026, 11, 4, tzinfo=UTC)

    def listing(district: str | None, zone: str | None, rooms: int = 2) -> Listing:
        return Listing(
            raw_message_id=None,
            deal_type="rent_out",
            category="house",
            city="nha_trang",
            title="Дом 2 комнаты",
            summary="Аренда",
            tg_link="https://example.test/house",
            posted_at=now - timedelta(hours=1),
            district=district,
            price_amount=Decimal(9_000_000),
            attributes={"rooms": rooms, **({"zone": zone} if zone else {})},
        )

    assert worth_sending(listing(None, "north"), wanted, now=now)
    assert not worth_sending(listing(None, None), wanted, now=now)
    assert worth_sending(listing("vinh_hai", "west"), wanted, now=now)
    assert worth_sending(listing("vinh_hai", None, 3), wanted, now=now)
    assert not worth_sending(listing("vinh_hai", None, 1), wanted, now=now)
