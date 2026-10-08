"""A housing follow-up must keep every condition without inventing verification."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sniffer.agent_app.main_gateway import collection_scope
from sniffer.bot.wording import accepted, result_header
from sniffer.domain.passport import Category
from sniffer.search.housing_preferences import current as current_housing_preferences
from sniffer.search.housing_preferences import effective_query
from sniffer.search.intake_rules import parse_query
from sniffer.search.plan import context_params
from sniffer.search.refinements import refine
from sniffer.search.relevance import rank_items
from sniffer.sources.base import RawItem

REQUEST = (
    "Уточни этот поиск аренды квартиры: Нячанг, бюджет строго до 6500000 VND в месяц. "
    "Ближе к ресторану Veranda на Trần Khát Chân, Đường Đệ, Vĩnh Hòa, север Нячанга. "
    "Нужны отдельная кухня, балкон, хороший современный ремонт, мебель и бытовая техника. "
    "Дата заезда гибкая, срок аренды пока не определён. "
    "Покажи подходящие варианты с исходными объявлениями"
)


def test_housing_followup_preserves_supported_and_unverified_conditions() -> None:
    current = parse_query("Сниму квартиру в Нячанге до 7000000 VND в месяц")
    fresh = parse_query(REQUEST)
    revised = refine(current, fresh, REQUEST)

    assert revised.category is Category.APARTMENT
    assert revised.city == "nha_trang"
    assert revised.budget.max == 6_500_000
    assert revised.attributes["balcony"] is True
    assert revised.attributes["furnished"] is True
    assert "Veranda" in revised.raw_query
    assert "бытовая техника" in revised.raw_query
    unverified = list(current_housing_preferences(revised.attributes).values())
    assert any("отдельная кухня" in preference for preference in unverified)
    assert any("современный ремонт" in preference for preference in unverified)
    assert any("бытовая техника" in preference for preference in unverified)
    assert any("Veranda" in preference for preference in unverified)
    assert revised.must_have == []  # Unverified prose must not become an archive hard filter.
    assert collection_scope(revised).criteria.must_have == ()
    assert "_unverified_housing_preferences" not in context_params(revised).get("attributes", {})
    without_wishes = revised.model_copy(
        update={
            "attributes": {
                key: value
                for key, value in revised.attributes.items()
                if not key.startswith("_unverified_")
            }
        }
    )
    assert collection_scope(revised).criteria.key == collection_scope(without_wishes).criteria.key
    assert "не провер" in accepted(revised).lower()
    assert "не провер" in result_header(revised, 1, 1).lower()
    later = refine(revised, parse_query("до 6000000 VND"), "до 6000000 VND")
    assert "Veranda" in later.raw_query
    assert "современный ремонт" in later.raw_query
    assert later.budget.max == 6_000_000


def test_new_housing_wish_replaces_or_cancels_old_one() -> None:
    first = parse_query("Сниму квартиру в Нячанге рядом с Veranda, отдельная кухня")
    second = refine(first, parse_query("Теперь без кухни, у моря"), "Теперь без кухни, у моря")
    wishes = current_housing_preferences(second.attributes)

    assert "kitchen" not in wishes
    assert wishes["location"] == "у моря"
    assert "Veranda" in second.raw_query  # original wording remains available for audit
    assert "Veranda" not in effective_query(second.raw_query, second.attributes)
    assert "без кухни" not in effective_query(second.raw_query, second.attributes)
    assert "отдельная кухня" not in accepted(second)
    assert "Veranda" not in accepted(second)


def test_repeated_edits_do_not_discard_active_wishes() -> None:
    revised = parse_query("Сниму квартиру в Нячанге рядом с Veranda, отдельная кухня")
    for number in range(12):
        text = f"до {6_000_000 + number} VND " + ("уточнение " * 70)
        revised = refine(revised, parse_query(text), text)

    assert "Veranda" in revised.raw_query
    assert "отдельная кухня" in revised.raw_query
    assert "Veranda" in current_housing_preferences(revised.attributes)["location"]


@pytest.mark.parametrize(
    ("query", "balcony"),
    [
        ("Сниму квартиру в Нячанге с балконом", True),
        ("Сниму квартиру в Нячанге без балкона", False),
    ],
)
def test_balcony_request_keeps_its_polarity(query: str, balcony: bool) -> None:
    assert parse_query(query).attributes["balcony"] is balcony


@pytest.mark.parametrize(
    "query",
    [
        "Сниму квартиру в Нячанге, балкон необязателен",
        "Сниму квартиру в Нячанге, балкона нет в требованиях",
        "Сниму квартиру в Нячанге, балкон не обязателен",
        "Сниму квартиру в Нячанге, не нужен балкон",
        "Сниму квартиру в Нячанге, балкон желательно, но не обязательно",
    ],
)
def test_optional_balcony_is_not_required(query: str) -> None:
    assert "balcony" not in parse_query(query).attributes


def test_housing_known_contradiction_excluded_and_missing_fact_unconfirmed() -> None:
    wanted = parse_query("Сниму квартиру в Нячанге с балконом и мебелью")
    now = datetime(2026, 10, 8, tzinfo=UTC)

    def listing(name: str, attributes: dict[str, object]) -> RawItem:
        return RawItem(
            source="synthetic",
            external_id=name,
            url=f"https://example.test/{name}",
            title=f"Квартира {name}",
            text=f"Сдаётся квартира {name} с уникальным описанием источника {name}",
            price_vnd=6_000_000,
            posted_at=now,
            raw={"attributes": attributes},
        )

    confirmed = listing("confirmed", {"balcony": True, "furnished": True})
    unknown = listing("unknown", {})
    contradicted = listing("contradicted", {"balcony": False, "furnished": True})
    ranked = rank_items(wanted, [unknown, contradicted, confirmed], usd_vnd=None, now=now)

    assert [item.external_id for item in ranked] == ["confirmed", "unknown"]
