"""Критерии слежения и диалога — одни (D3): общий построитель, умолчание «бензин», полоса объёма."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from sniffer.domain.match_filter import EXACT_ATTRIBUTES, build_match_filter, ceiling_vnd
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.domain.records import Listing
from sniffer.matching import filter_for, worth_sending
from sniffer.matching.rules import MONITOR_MAX_AGE

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def passport(**overrides: Any) -> Passport:
    fields: dict[str, Any] = {
        "intent": Intent.BUY,
        "category": Category.MOTORBIKE,
        "city": "nha_trang",
    }
    fields.update(overrides)
    return Passport(**fields)


def card(*, hours_old: float = 1, price: int | None = None, **attributes: Any) -> Listing:
    return Listing(
        id=1,
        raw_message_id=1,
        deal_type="sell",
        category="motorbike",
        city="nha_trang",
        title="Honda Vision",
        summary="Автомат",
        tg_link="https://t.me/c/1/1",
        posted_at=NOW - timedelta(hours=hours_old),
        price_amount=Decimal(price) if price is not None else None,
        attributes=attributes,
    )


def test_the_monitor_builds_the_same_filter_as_the_dialog_path() -> None:
    asked = passport(attributes={"engine_cc": 200, "brand": "honda", "model": "vision"})
    monitor = filter_for(asked, now=NOW)
    assert monitor is not None
    dialog = build_match_filter(
        city="nha_trang",
        category="motorbike",
        intent=Intent.BUY,
        ceiling=None,
        since=monitor.since or NOW,
        attributes={"engine_cc": 200, "brand": "honda", "model": "vision"},
    )
    assert monitor == dialog


def test_a_bike_without_the_word_electric_is_petrol_for_the_monitor_too() -> None:
    spec = filter_for(passport(), now=NOW)
    assert spec is not None
    assert spec.attributes == {"power": "fuel"}


def test_an_electric_bike_is_not_sent_to_a_petrol_subscriber() -> None:
    assert worth_sending(card(power="electric"), passport(), now=NOW) is False
    assert worth_sending(card(power="fuel"), passport(), now=NOW) is True


def test_an_electric_request_gets_electric_bikes() -> None:
    wants_electric = passport(attributes={"power": "electric"})
    assert worth_sending(card(power="electric"), wants_electric, now=NOW) is True
    assert worth_sending(card(power="fuel"), wants_electric, now=NOW) is False


def test_an_unknown_power_does_not_block_a_card() -> None:
    assert worth_sending(card(), passport(), now=NOW) is True


@pytest.mark.parametrize(
    ("cc", "ok"), [(175, True), (250, True), (150, True), (125, False), (700, False)]
)
def test_a_named_volume_is_a_band_not_an_exact_match(cc: int, ok: bool) -> None:
    asked = passport(attributes={"engine_cc": 200})
    assert worth_sending(card(engine_cc=cc), asked, now=NOW) is ok


def test_the_band_reaches_the_query_filter_too() -> None:
    spec = filter_for(passport(attributes={"engine_cc": 200}), now=NOW)
    assert spec is not None
    assert (spec.engine_cc_min, spec.engine_cc_max) == (150, 250)


@pytest.mark.parametrize(("cc", "ok"), [(300, True), (250, True), (200, False)])
def test_a_lower_bound_means_at_least(cc: int, ok: bool) -> None:
    asked = passport(attributes={"engine_cc": 250, "engine_cc_dir": "min"})
    assert worth_sending(card(engine_cc=cc), asked, now=NOW) is ok


def test_an_unknown_volume_does_not_block_a_card() -> None:
    assert worth_sending(card(), passport(attributes={"engine_cc": 200}), now=NOW) is True


def test_an_old_card_with_a_fresh_id_is_not_new() -> None:
    # Пересчёт или добор архива выдаёт старой публикации новый id; «новое» она не становится.
    # Цена в бюджете даёт оценку выше порога при любой давности — возраст держит отдельное условие.
    priced = passport(budget=Budget(max=30_000_000, currency=Currency.VND))
    edge = MONITOR_MAX_AGE.total_seconds() / 3600
    assert worth_sending(card(hours_old=edge - 0.1, price=10_000_000), priced, now=NOW) is True
    assert worth_sending(card(hours_old=edge + 0.1, price=10_000_000), priced, now=NOW) is False
    assert worth_sending(card(hours_old=30 * 24, price=10_000_000), priced, now=NOW) is False


def test_the_dollar_budget_becomes_a_dong_ceiling_with_the_rate() -> None:
    asked = passport(budget=Budget(max=300, currency=Currency.USD))
    spec = filter_for(asked, usd_vnd=26_000, now=NOW)
    assert spec is not None
    assert spec.max_price_vnd == Decimal(str(300 * 26_000.0))


@pytest.mark.parametrize("currency", [Currency.EUR, Currency.RUB])
def test_eur_and_rub_budgets_give_no_ceiling_anywhere(currency: Currency) -> None:
    # Курса для них нет ни у диалога, ни у слежения: честнее не сужать, чем сужать выдуманным.
    assert ceiling_vnd(Budget(max=500, currency=currency), 26_000) is None
    spec = filter_for(passport(budget=Budget(max=500, currency=currency)), usd_vnd=26_000, now=NOW)
    assert spec is not None and spec.max_price_vnd is None


def test_the_dong_budget_is_taken_as_is_and_a_missing_budget_gives_none() -> None:
    assert ceiling_vnd(Budget(max=15_000_000, currency=Currency.VND), None) == Decimal("15000000")
    assert ceiling_vnd(Budget(), 26_000) is None


def test_the_exact_attribute_list_is_the_one_the_dialog_used() -> None:
    assert EXACT_ATTRIBUTES == ("brand", "transmission", "rooms", "power")


def test_a_named_volume_does_not_dilute_the_match_score() -> None:
    # Объём судится полосой (`worth_sending`), а не равенством: считать его «несовпавшим»
    # атрибутом в оценке значило бы снижать оценку карточки, которую слежение уже признало.
    from sniffer.matching import score

    branded = passport(attributes={"brand": "honda"})
    with_volume = passport(attributes={"brand": "honda", "engine_cc": 200})
    one = card(brand="honda", engine_cc=175)
    assert score(one, with_volume, now=NOW) == score(one, branded, now=NOW)
