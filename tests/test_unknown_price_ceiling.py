"""Слежение не шлёт карточку без колонки цены, если все суммы в тексте выше потолка."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.domain.records import Listing
from sniffer.matching.rules import worth_sending

NOW = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
# Текст карточки, ушедшей 27.11.2026 подписке с потолком 14 млн: «в день» вместо «в месяц».
HON_XEN = (
    "🏡 Дом в аренду в Хон Ксен - Северный Нячанг 🔷 - 3 спальни, 3 ванные комнаты "
    "💰 Цена: 25 000 000 VND/день - Договор: от 10 месяцев до 1 года "
    "- Предоплата за 2 месяца - Залог за 2 месяца"
)


def _house(summary: str, *, price: Decimal | None = None) -> Listing:
    return Listing(
        raw_message_id=1,
        deal_type="rent_out",
        category="house",
        city="nha_trang",
        title="Дом в аренду",
        summary=summary,
        tg_link="https://t.me/c/1/1",
        posted_at=NOW,
        price_amount=price,
        attributes={},
    )


def _wish(maximum: float | None = 14_000_000) -> Passport:
    return Passport(
        intent=Intent.RENT,
        category=Category.HOUSE,
        city="nha_trang",
        budget=Budget(max=maximum, currency=Currency.VND),
    )


def test_a_daily_price_above_the_monthly_ceiling_is_not_sent() -> None:
    assert not worth_sending(_house(HON_XEN), _wish(), now=NOW)


def test_a_text_price_within_the_ceiling_still_goes() -> None:
    text = "Дом 3 спальни. Цена: 13 млн VND/месяц. Залог: 30 000 000 VND"
    assert worth_sending(_house(text), _wish(), now=NOW)


def test_a_listing_without_any_price_still_goes() -> None:
    assert worth_sending(_house("Дом 3 спальни, сад, звоните"), _wish(), now=NOW)


def test_no_ceiling_no_cut() -> None:
    assert worth_sending(_house(HON_XEN), _wish(None), now=NOW)
