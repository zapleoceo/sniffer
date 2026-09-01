"""Бюджет и паспорт должны менять карточки, а не оставаться текстом в логе."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.search.plan import SearchPlan, SearchTask
from sniffer.search.relevance import rank_items, with_vnd_budget
from sniffer.sources.base import RawItem

NOW = datetime(2026, 9, 1, tzinfo=UTC)
RATE = 26_000.0


def passport(**changes: Any) -> Passport:
    values: dict[str, Any] = {
        "intent": Intent.BUY,
        "category": Category.MOTORBIKE,
        "city": "nha_trang",
        "budget": Budget(max=500, currency=Currency.USD),
        "attributes": {"transmission": "automatic"},
    }
    values.update(changes)
    return Passport(**values)


def item(
    name: str,
    *,
    price: int | None,
    text: str = "",
    age_hours: int = 1,
) -> RawItem:
    return RawItem(
        source="telegram_groups",
        external_id=name,
        url=f"https://example.test/{name}",
        title=name,
        text=text,
        price_vnd=price,
        posted_at=NOW - timedelta(hours=age_hours),
    )


def test_usd_budget_becomes_vnd_before_chotot_request() -> None:
    plan = SearchPlan(tasks=[SearchTask(source="chotot", query="", params={"budget": {}})])

    converted = with_vnd_budget(plan, passport(), RATE)

    assert converted.tasks[0].params["budget"] == {
        "min": 0.0,
        "max": 13_000_000.0,
        "currency": "VND",
        "period": "month",
    }


def test_budget_beats_recency_in_card_order() -> None:
    expensive = item("new but too expensive", price=18_000_000, age_hours=1)
    suitable = item("older but suitable", price=12_000_000, age_hours=24)

    ordered = rank_items(passport(), [expensive, suitable], usd_vnd=RATE, now=NOW)

    assert [candidate.external_id for candidate in ordered] == [
        "older but suitable",
        "new but too expensive",
    ]


def test_known_automatic_text_beats_unconfirmed_variant() -> None:
    confirmed = item("automatic", price=12_000_000, text="Xe tay ga Honda", age_hours=12)
    unknown = item("unknown", price=12_000_000, text="Honda 125", age_hours=1)

    ordered = rank_items(passport(), [unknown, confirmed], usd_vnd=RATE, now=NOW)

    assert [candidate.external_id for candidate in ordered] == ["automatic", "unknown"]


def test_missing_rate_never_invents_price_filter() -> None:
    original = SearchPlan(tasks=[SearchTask(source="chotot", query="")])

    assert with_vnd_budget(original, passport(), None) == original
