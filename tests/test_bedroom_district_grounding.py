"""Bedroom and district evidence stays distinct from total rooms and guesses."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from sniffer.bot.wording import accepted
from sniffer.db.mappers import _legacy_housing_attributes
from sniffer.domain.housing_rooms import read_bedrooms, read_total_rooms
from sniffer.domain.records import Listing
from sniffer.matching.rules import worth_sending
from sniffer.search.intake import intake_schema, merge
from sniffer.search.intake_rules import parse_query
from sniffer.search.relevance import _wrong_bedrooms, rank_items
from sniffer.sources.base import RawItem
from sniffer.verifier.offer_screen import parse_verdicts, screen_schema
from sniffer.worker.screening import screened_fields


def test_four_bedrooms_is_not_four_total_rooms() -> None:
    wanted = parse_query("Дом, север Нячанга, 4 спальни, до 10 млн")
    assert wanted.districts
    assert wanted.attributes["bedrooms"] == 4
    assert "rooms" not in wanted.attributes
    assert "от 4 спален" in accepted(wanted)
    assert read_total_rooms("Дом 4 комнаты: 3 спальни и гостиная") == 4
    assert read_bedrooms("Дом 4 комнаты: 3 спальни и гостиная") == 3
    assert read_bedrooms("Nhà 3PN") == 3
    item = RawItem("archive", "1", "https://example.test/1", raw={})
    assert _wrong_bedrooms(item, "Дом 4 комнаты: 3 спальни и гостиная", 4)
    assert _wrong_bedrooms(item, "Nhà 3PN", 4)
    assert _wrong_bedrooms(item, "Дом 4 комнаты", 4)
    assert not _wrong_bedrooms(item, "Nhà 4 phòng ngủ", 4)


def test_saved_legacy_passport_is_interpreted_without_mutating_its_row() -> None:
    stored = {"rooms": 4, "rooms_min": True}
    normalized = _legacy_housing_attributes(stored, "Дом, 4 спальни")
    assert normalized == {"bedrooms": 4}
    assert stored == {"rooms": 4, "rooms_min": True}
    assert _legacy_housing_attributes({"rooms": 4}, "4 комнаты, 3 спальни") == {
        "rooms": 4,
        "bedrooms": 3,
    }


def test_monitor_requires_proven_bedrooms_and_northern_district() -> None:
    wanted = parse_query("Дом, север Нячанга, 4 спальни, до 10 млн")
    now = datetime(2026, 11, 4, tzinfo=UTC)
    item = Listing(
        id=1,
        raw_message_id=None,
        deal_type="rent_out",
        category="house",
        city="nha_trang",
        district="vinh_hai",
        title="Дом 4 комнаты",
        summary="север Нячанга",
        tg_link="https://t.me/c/1/1",
        posted_at=now,
    )
    assert not worth_sending(item, wanted, now=now)
    assert not worth_sending(replace(item, attributes={"bedrooms": 3}), wanted, now=now)
    assert worth_sending(replace(item, attributes={"bedrooms": 5}), wanted, now=now)
    assert not worth_sending(
        replace(item, district="phuoc_hai", attributes={"bedrooms": 5}), wanted, now=now
    )


def test_query_model_fields_are_grounded_in_the_original_words() -> None:
    schema = intake_schema()
    assert set(schema["required"]) == set(schema["properties"])
    original = parse_query("Дом, север Нячанга, 4 спальни, до 10 млн")
    confirmed = merge(
        original, {"district_evidence": "север Нячанга", "bedrooms_evidence": "4 спальни"}
    )
    assert confirmed.districts == original.districts
    assert confirmed.attributes["bedrooms"] == 4
    ungrounded = merge(
        parse_query("Дом в Нячанге до 10 млн"),
        {"district_evidence": "север Нячанга", "bedrooms_evidence": "4 спальни"},
    )
    assert ungrounded.districts == []
    assert "bedrooms" not in ungrounded.attributes


def test_candidate_model_evidence_is_checked_against_the_listing_text() -> None:
    schema = screen_schema()["properties"]["verdicts"]["items"]
    assert set(schema["required"]) == set(schema["properties"])
    source = "Дом 4 комнаты: 3 спальни и гостиная, север Нячанга"
    row = {
        "n": 1,
        "kind": "offer",
        "category": "house",
        "deal": "rent_out",
        "power": "unknown",
        "brand": "",
        "engine_cc": "",
        "rooms": "4",
        "why": "",
        "district_evidence": "север Нячанга",
        "bedrooms_evidence": "3 спальни",
    }
    verdict = parse_verdicts({"verdicts": [row]}, 1, texts=[source])[0]
    assert verdict is not None
    assert verdict.zone == "north" and verdict.bedrooms == 3
    listing = Listing(
        id=1,
        raw_message_id=None,
        deal_type="rent_out",
        category="house",
        city="nha_trang",
        title="Дом 4 комнаты: 3 спальни и гостиная",
        summary="север Нячанга",
        tg_link="https://t.me/c/1/1",
        posted_at=datetime(2026, 11, 4, tzinfo=UTC),
    )
    fields = screened_fields(listing, verdict)
    assert fields["attributes"]["bedrooms"] == 3
    assert fields["attributes"]["rooms"] == 4
    without_model_quote = parse_verdicts(
        {"verdicts": [{**row, "bedrooms_evidence": ""}]}, 1, texts=[source]
    )[0]
    assert without_model_quote is not None
    fallback = screened_fields(listing, without_model_quote)
    assert fallback["attributes"]["bedrooms"] == 3
    assert fallback["attributes"]["rooms"] == 4
    invented = parse_verdicts(
        {"verdicts": [{**row, "bedrooms_evidence": "4 спальни"}]}, 1, texts=[source]
    )[0]
    assert invented is not None and invented.bedrooms is None


def test_search_ranking_rejects_wrong_or_unproven_bedroom_counts() -> None:
    wanted = parse_query("Дом, север Нячанга, 4 спальни, до 10 млн")
    examples = [
        RawItem(
            "archive",
            str(n),
            f"https://example.test/{n}",
            title=title,
            text=f"{body} #{n}",
            price_vnd=8_000_000,
            raw={"attributes": {"zone": zone}},
        )
        for n, title, body, zone in (
            (1, "Дом 4 комнаты: 3 спальни и гостиная", "север Нячанга", "north"),
            (2, "Nhà 3PN", "север Нячанга", "north"),
            (3, "Дом 4 комнаты", "север Нячанга", "north"),
            (4, "Дом 4 спальни", "север Нячанга", "north"),
            (5, "Дом 4 спальни", "Западный Нячанг", "west"),
        )
    ]
    assert [item.external_id for item in rank_items(wanted, examples, usd_vnd=None)] == ["4"]
