"""Слот с жёстким фильтром ставит в очередь только карточки, прошедшие все условия.

Подписка без фильтра ведёт себя прежним мягким отбором — вторая половина проверки: поведение
остальных клиентов не меняется.
"""

from __future__ import annotations

import pytest

from sniffer.domain.hard_filter import BALCONY, SEPARATE_KITCHEN, HardFilter
from sniffer.domain.passport import Budget, Category, Currency, Intent
from sniffer.domain.records import Listing, StoredPassport, SubscriptionState
from sniffer.worker.monitor import MonitorAgent
from tests.monitor_support import NOW, install, listing, passport, subscription

STRICT = HardFilter(
    require=frozenset({BALCONY, SEPARATE_KITCHEN}),
    districts=frozenset({"vinh_hoa"}),
)


def flat(number: int, summary: str, district: str | None = "vinh_hoa") -> Listing:
    return listing(
        number,
        category="apartment",
        deal_type="rent_out",
        summary=summary,
        district=district,
        price_amount=6_000_000,
        price_currency="VND",
        price_period="month",
    )


def rental(hard_filter: HardFilter | None) -> SubscriptionState:
    stored = StoredPassport(
        id=201,
        user_id=101,
        version=1,
        passport=passport(
            intent=Intent.RENT,
            category=Category.APARTMENT,
            budget=Budget(max=6_500_000, currency=Currency.VND),
        ),
    )
    return subscription(1, passport=stored, hard_filter=hard_filter)


PAGE = [
    flat(1, "Отдельная кухня, балкон"),
    flat(2, "Балкон, кухня совмещена с гостиной"),
    flat(3, "Отдельная кухня, балкон", district="phuoc_long"),
    flat(4, "Тихая квартира"),
]


async def test_only_cards_with_every_condition_and_the_place_are_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = install(monkeypatch, subscriptions=[rental(STRICT)], page=PAGE)

    assert await MonitorAgent().tick(now=NOW) == 1

    assert [item["listing_id"] for item in world.delivery.queued] == [1]
    assert world.delivery.advanced == [(1, 4)], "курсор уходит за отсеянные: к ним не вернёмся"


async def test_a_subscription_without_a_hard_filter_keeps_the_soft_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = install(monkeypatch, subscriptions=[rental(None)], page=PAGE)

    assert await MonitorAgent().tick(now=NOW) == 4

    assert [item["listing_id"] for item in world.delivery.queued] == [1, 2, 3, 4]
