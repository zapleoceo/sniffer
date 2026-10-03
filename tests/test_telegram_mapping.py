from __future__ import annotations

from types import SimpleNamespace

import pytest

from sniffer.domain.prices import price_hint
from sniffer.sources.telegram_mapping import PriceContext, price_context, to_item


@pytest.mark.parametrize(
    ("text", "shown", "price"),
    [
        ("Цена 22 мил. Писать в личку", "Цена 22 мил.", 22_000_000),
        ("Цена - 3 млн донгов. Блюкард есть", "Цена - 3 млн донгов", 3_000_000),
        ("price: 15.500.000 VND", "price: 15.500.000 VND", 15_500_000),
        ("Giá 3.7tr", "Giá 3.7tr", 3_700_000),
    ],
)
def test_price_hint_understands_real_group_price_forms(text: str, shown: str, price: int) -> None:
    assert price_hint(text) == (shown, price)


@pytest.mark.parametrize(
    "text",
    ["Honda 125cc, 2021 год", "Пробег 22 тыс., цена договорная", "Цена уточняйте"],
)
def test_price_hint_never_mistakes_engine_or_mileage_for_price(text: str) -> None:
    assert price_hint(text) == ("", None)


CHAT = SimpleNamespace(tg_id=-1001234567890, username="nhatrang_realestate", title="Барахолка")


def _post(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=7, message=text, date=None, media=None, grouped_id=None, reply_to=None
    )


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"category": "apartment", "intent": "rent"}, PriceContext("apartment", "rent_out")),
        ({"category": "motorbike", "intent": "buy"}, PriceContext("motorbike", "sell")),
        # Продающему и сдающему предложений нет: стороны у цен нет, и гадать не из чего.
        ({"category": "motorbike", "intent": "sell"}, PriceContext("motorbike", None)),
        ({"category": "house", "intent": "rent_out"}, PriceContext("house", None)),
        ({"intent": "bogus", "category": "  "}, PriceContext()),
        ({}, PriceContext()),
    ],
)
def test_the_plan_gives_the_price_reader_the_category_and_the_side_of_the_offers(
    params: dict[str, str], expected: PriceContext
) -> None:
    assert price_context(params) == expected


def test_a_fee_is_not_the_price_once_the_plan_knows_the_category() -> None:
    """Живая находка 92319: рядом с арендой в 17 млн стоит сбор «700 000 донгов в месяц»."""
    post = _post("Стоимость аренды: 17\n💵 Условия: 1 месяц + депозит\n700 000 донгов в месяц")

    blind = to_item(CHAT, post)
    informed = to_item(CHAT, post, PriceContext("apartment", "rent_out"))

    assert blind is not None
    assert informed is not None
    assert informed.price_vnd is None
    assert informed.price_raw == ""


def test_a_bare_number_gets_its_scale_from_the_category_the_plan_names() -> None:
    post = _post("Продам NVX, цена 19.800")

    blind = to_item(CHAT, post)
    informed = to_item(CHAT, post, PriceContext("motorbike", "sell"))

    assert blind is not None
    assert blind.price_vnd is None
    assert informed is not None
    assert informed.price_vnd == 19_800_000


def test_a_daily_rate_never_becomes_the_dong_price_of_a_rent_search() -> None:
    post = _post("Honda Vision 2019, 250 000 VND/сутки")

    item = to_item(CHAT, post, PriceContext("motorbike", "rent_out"))

    assert item is not None
    assert (item.price_vnd, item.price_raw) == (None, "")
