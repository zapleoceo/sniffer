from __future__ import annotations

import pytest

from sniffer.domain.prices import price_hint


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
