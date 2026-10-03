"""Цена карточки: что уходит в колонки бюджета, а что остаётся в атрибутах."""

from __future__ import annotations

from decimal import Decimal

from sniffer.domain.prices import PriceFact
from sniffer.pipeline.listing_price import PriceColumns, price_columns


def fact(
    amount: int, *, currency: str = "VND", period: str | None = None, up_to: int | None = None
) -> PriceFact:
    return PriceFact("raw", amount, currency, period, "label", up_to)


def test_no_price_leaves_every_column_empty() -> None:
    assert price_columns(None, "rent_out") == PriceColumns()


def test_a_monthly_rent_is_a_monthly_dong_price() -> None:
    columns = price_columns(fact(9_000_000, period="month"), "rent_out")

    assert (columns.amount, columns.currency, columns.period) == (
        Decimal(9_000_000),
        "VND",
        "month",
    )
    assert columns.attributes == {}


def test_a_rent_without_a_stated_period_is_monthly() -> None:
    """Сдают помесячно: «13 млн» без слова «мес» — это тоже месяц."""
    assert price_columns(fact(13_000_000), "rent_out").period == "month"


def test_a_sale_price_is_once() -> None:
    columns = price_columns(fact(24_000_000), "sell")

    assert (columns.amount, columns.period) == (Decimal(24_000_000), "once")


def test_the_upper_bound_of_a_range_is_kept_in_the_attributes() -> None:
    columns = price_columns(fact(9_000_000, period="month", up_to=11_000_000), "rent_out")

    assert columns.attributes == {"price_up_to": 11_000_000}


def test_a_daily_rent_never_enters_the_monthly_budget_column() -> None:
    """250 тысяч в сутки, лежащие в `price_amount`, прошли бы любой месячный бюджет."""
    columns = price_columns(fact(250_000, period="day"), "rent_out")

    assert columns.amount is None
    assert columns.attributes == {
        "rate_amount": 250_000,
        "rate_currency": "VND",
        "rate_per": "day",
    }


def test_dollars_never_pose_as_dong() -> None:
    columns = price_columns(fact(2_000, currency="USD"), "rent_out")

    assert (columns.amount, columns.currency) == (None, None)
    assert columns.attributes["rate_currency"] == "USD"
    assert columns.attributes["rate_per"] == "month"


def test_the_upper_bound_of_a_daily_range_stays_next_to_the_rate() -> None:
    """«от 250 до 400 тыс в сутки» — вилка ставки: верх держится в атрибутах, а не теряется."""
    columns = price_columns(fact(250_000, period="day", up_to=400_000), "rent_out")

    assert columns.attributes["rate_up_to"] == 400_000
    assert "price_up_to" not in columns.attributes
