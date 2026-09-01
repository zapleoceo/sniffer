from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sniffer.domain.records import Chat, RawMessage
from sniffer.pipeline.archive import STAGE_REJECTED, classify, listing_from


@pytest.fixture
def chat() -> Chat:
    return Chat(tg_id=-1001234567890, username="nha_trang_flea", title="Flea", city="nha_trang")


def raw(text: str, *, identifier: int = 7) -> RawMessage:
    return RawMessage(
        id=identifier,
        chat_tg_id=-1001234567890,
        msg_id=88,
        text=text,
        text_hash="hash",
        posted_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


def test_passing_archive_message_becomes_linked_listing(chat: Chat) -> None:
    message = raw("Продам Yamaha Nouvo, цена — 3.7tr, документы есть")

    listing = listing_from(message, chat, classify(message))

    assert listing.category == "motorbike"
    assert listing.city == "nha_trang"
    assert listing.price_amount == 3_700_000
    assert listing.tg_link == "https://t.me/nha_trang_flea/88"


@pytest.mark.parametrize(
    "text",
    ["Ищу скутер до 5 млн, подскажите", "Honda Vision 2021, состояние отличное"],
)
def test_demand_and_chatter_never_become_listings(text: str) -> None:
    result = classify(raw(text))

    assert not result.passed
    assert result.reason in {"demand_not_offer", "no_price_no_offer_verb"}
    assert STAGE_REJECTED == "rejected"


def test_private_chat_link_uses_internal_telegram_id() -> None:
    message = raw("Продам Honda Vision, цена 22 млн")
    chat = Chat(tg_id=-1001234567890, title="Private", city="nha_trang")

    listing = listing_from(message, chat, classify(message))

    assert listing.tg_link == "https://t.me/c/1234567890/88"
