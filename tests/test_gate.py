"""Гейт — единственная ступень воронки, которая работает без сети и без LLM,
поэтому её поведение фиксируется тестами на реальных формулировках из чатов.
"""

from __future__ import annotations

import pytest

from sniffer.domain.passport import Category
from sniffer.pipeline.gate import gate

OFFERS = [
    "Продам байк Honda Vision 2022, пробег 8000, 350$, Нячанг, документы есть",
    "Сдается квартира 1 спальня в Muong Thanh, 400$/месяц, есть бассейн и зал",
    "Honda Air Blade 2021 в отличном состоянии, 7 triệu, торг уместен, район Vinh Hai",
    "For rent: studio apartment near the beach, 350 usd per month, fully furnished",
    "Cho thuê căn hộ 2 phòng ngủ, 8.500.000 VND, có máy lạnh",
]

NOT_OFFERS = [
    ("Ищу байк в аренду на месяц, бюджет до 100$", "demand_not_offer"),
    ("Всем привет, кто знает хорошего стоматолога?", "no_price_no_offer_verb"),
    ("Спасибо!", "too_short"),
    ("Продам холодильник Samsung, 200$, самовывоз", "no_category_hint"),
]


@pytest.mark.parametrize("text", OFFERS)
def test_real_offers_pass(text: str) -> None:
    result = gate(text)
    assert result.passed, f"отброшено: {result.reason}"
    assert result.categories


@pytest.mark.parametrize(("text", "reason"), NOT_OFFERS)
def test_noise_rejected(text: str, reason: str) -> None:
    result = gate(text)
    assert not result.passed
    assert result.reason == reason


def test_price_without_verb_still_passes() -> None:
    """«Honda Vision 2022, 350$» — глагола сделки нет, но это объявление."""
    result = gate("Honda Vision 2022, пробег 12000 км, 350$, Нячанг, район Loc Tho")
    assert result.passed
    assert result.has_price
    assert not result.is_offer
    assert Category.MOTORBIKE in result.categories


def test_demand_and_offer_are_told_apart_by_order() -> None:
    """«в аренду» есть в обеих формулировках — различает только порядок."""
    assert not gate("Ищу байк в аренду на месяц, бюджет до 100$").passed
    assert gate("Сдам в аренду байк Honda, 100$/мес, ищу аккуратного арендатора").passed


def test_signals_are_serialisable() -> None:
    signals = gate(OFFERS[0]).as_signals()
    assert signals["has_price"] is True
    assert "motorbike" in signals["categories"]  # type: ignore[operator]
