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

    assert [candidate.external_id for candidate in ordered] == ["older but suitable"]


def test_known_automatic_text_beats_unconfirmed_variant() -> None:
    confirmed = item("automatic", price=12_000_000, text="Xe tay ga Honda", age_hours=12)
    unknown = item("unknown", price=12_000_000, text="Honda 125", age_hours=1)

    ordered = rank_items(passport(), [unknown, confirmed], usd_vnd=RATE, now=NOW)

    assert [candidate.external_id for candidate in ordered] == ["automatic", "unknown"]


def test_missing_rate_never_invents_price_filter() -> None:
    original = SearchPlan(tasks=[SearchTask(source="chotot", query="")])

    assert with_vnd_budget(original, passport(), None) == original


# ── чужая категория до карточек не доходит ─────────────────────────────────


def test_another_category_does_not_reach_the_cards() -> None:
    """Комната и велосипед в выдаче скутеров — 41% показанного в замере 02.09.2026.

    Поиск по чату идёт словами, структурного поля категории у чата нет, а
    модельный фильтр такой лот не видит: комната модели не называет. В пятёрку
    она проходила по свежести.
    """
    room = item("Комната с общей кухней, район Винком", price=4_500_000)
    bike = item("Велосипед Giant Escape 3, почти новый", price=3_200_000)
    scooter = item("Honda Vision 2019, скутер", price=9_800_000, age_hours=48)

    ordered = rank_items(passport(), [room, bike, scooter], usd_vnd=RATE, now=NOW)

    assert [candidate.external_id for candidate in ordered] == [scooter.external_id]


def test_a_lot_whose_category_is_unreadable_is_not_thrown_away() -> None:
    """«Не смогли прочитать» — это не «не подходит».

    То же правило, что у модели: продавец не обязан называть предмет словом из
    нашего словаря, и выбрасывать за это значит терять половину чата.
    """
    silent = item("Honda Vision 2019, автомат, один хозяин", price=9_800_000)

    assert rank_items(passport(), [silent], usd_vnd=RATE, now=NOW) == [silent]


def test_an_empty_result_does_not_bring_the_foreign_category_back() -> None:
    """Ступень не отменяется при пустом результате — в отличие от возраста.

    Возраст — догадка о живости, чужая категория — факт о предмете, прочитанный
    из его собственных слов. Комната вместо скутера, когда скутеров нет, — это
    ровно та жалоба, ради которой отсев и появился: пустой ответ хотя бы
    советует переформулировать.
    """
    room = item("Сдам комнату, район Винком", price=4_500_000)

    assert rank_items(passport(), [room], usd_vnd=RATE, now=NOW) == []


def test_a_request_without_a_category_filters_nothing() -> None:
    """Клиент не назвал предмет — сравнивать не с чем, и выбрасывать не за что."""
    room = item("Комната с общей кухней", price=4_500_000)

    assert rank_items(passport(category=None), [room], usd_vnd=RATE, now=NOW) == [room]
