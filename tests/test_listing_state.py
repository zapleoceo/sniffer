"""Когда объявление перестаёт быть предложением: слова продавца и возраст."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sniffer.domain.listing_state import LISTING_MAX_AGE_DAYS, announces_closed
from sniffer.pipeline.gate import gate
from sniffer.worker.expiry import Expiry


@pytest.mark.parametrize(
    "text",
    [
        "ПРОДАНО! Honda Lead 2019",
        "Байк продан, спасибо всем",
        "Квартира сдана, объявление не актуально",
        "Неактуально",
        "SOLD. Yamaha NVX",
        "Honda Vision đã bán",
        "Căn hộ đã cho thuê",
        # Правка автора: слово в начале текста или строки, дальше что угодно.
        "ПРОДАНО Honda Vision",
        "Продам Honda Lead 2019, 12 млн, документы.\nПРОДАНО",
        "❗️SOLD❗️ Yamaha NVX 2020, 25tr, blue card, full documents, helmet included",
        "Đã bán! Honda Vision 2019, xe đẹp, giá 25tr, giấy tờ đầy đủ, còn bảo hành",
        # Короткая реплика целиком, слово отдельной фразой.
        "Xe đã bán rồi",
        "Квартира сдана.",
        "Yamaha NVX - sold out",
    ],
)
def test_the_seller_closing_the_offer_is_recognised(text: str) -> None:
    assert announces_closed(text)


@pytest.mark.parametrize(
    "text",
    [
        "Продам Honda Lead 2019, 12 млн",
        "Продаю скутер, срочно",
        "Сдаётся студия у моря, 10 млн",
        "Сдам квартиру на длительный срок",
        # Свойство новостройки, а не закрытое объявление.
        "Дом сдан в эксплуатацию в 2023, сдам квартиру 2 спальни",
        "Аренда байков, продажа запчастей",
        # Слово посреди предложения не закрывает его (раньше закрывало где угодно).
        "Sold with helmet",
        "Honda Vision 2020, 15tr. Sold with helmet and cover, full documents, great condition",
        "Продаю, не sold out",
        "Honda Lead 2019, 25 млн. Ещё не продано, звоните, документы в порядке, торг",
        "Soldering iron, 100k",
        "Honda Vision 2020, 15tr, xe đã bán bảo hiểm đầy đủ cho người mua, giá tốt, cho thuê",
        "Продам байк, предыдущий владелец сдан в армию, поэтому недорого, документы есть",
    ],
)
def test_an_offer_is_not_mistaken_for_a_closed_one(text: str) -> None:
    assert not announces_closed(text)


def test_the_gate_never_turns_a_closed_offer_into_a_card() -> None:
    result = gate("ПРОДАНО. Honda Air Blade 2016, цена 9 млн, документы")

    assert not result.passed
    assert result.reason == "closed_offer"


async def test_expiry_retires_listings_older_than_their_lifetime_in_batches() -> None:
    now = datetime(2026, 9, 18, tzinfo=UTC)
    moments = [0.0]
    calls: list[tuple[datetime, int]] = []
    answers = [5, 2]

    async def expire(older_than: datetime, limit: int) -> int:
        calls.append((older_than, limit))
        return answers.pop(0) if answers else 0

    expiry = Expiry(
        batch=5, every_s=3600, expire=expire, now=lambda: now, monotonic=lambda: moments[0]
    )

    assert await expiry.tick() == 5, "полная пачка — продолжаем без паузы"
    assert await expiry.tick() == 2
    assert await expiry.tick() == 0, "пачка неполная — до следующего часа"
    assert calls[0] == (now - timedelta(days=LISTING_MAX_AGE_DAYS), 5)
    moments[0] += 3600
    await expiry.tick()
    assert len(calls) == 3
