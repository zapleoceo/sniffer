"""Дубль на входе: карточка остаётся на первоисточнике, цена — от свежей копии."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sniffer.domain.records import Chat, Listing, RawMessage
from sniffer.pipeline.archive import is_primary, primary_link
from sniffer.pipeline.duplicates import merge_values

T0 = datetime(2026, 11, 1, tzinfo=UTC)


def listing(raw_id: int, hours: int, link: str, price: int | None = 6_000_000) -> Listing:
    return Listing(
        raw_message_id=raw_id,
        deal_type="rent_out",
        category="apartment",
        city="nha_trang",
        title="Студия",
        summary=f"текст {raw_id}",
        tg_link=link,
        posted_at=T0 + timedelta(hours=hours),
        primary_posted_at=T0 + timedelta(hours=hours),
        external_id=f"-100:{raw_id}",
        price_amount=Decimal(price) if price is not None else None,
        price_currency="VND" if price is not None else None,
        price_period="month" if price is not None else None,
        id=raw_id * 10,
    )


def test_a_newer_repost_brings_price_and_freshness_but_not_its_link() -> None:
    primary = listing(1, 0, "https://t.me/a/1")
    repost = listing(2, 48, "https://t.me/b/2", price=5_500_000)

    values = merge_values(primary, repost)

    assert "tg_link" not in values and "raw_message_id" not in values
    assert values["price_amount"] == Decimal(5_500_000)
    assert values["posted_at"] == repost.posted_at


def test_an_earlier_copy_takes_over_the_link_and_keeps_the_newer_price() -> None:
    """Догон истории приносит ранний пост позже: карточка переезжает на первоисточник."""
    card = listing(2, 48, "https://t.me/b/2", price=5_500_000)
    earlier = listing(1, 0, "https://t.me/a/1")

    values = merge_values(card, earlier)

    assert values["tg_link"] == "https://t.me/a/1" and values["raw_message_id"] == 1
    assert "price_amount" not in values and "posted_at" not in values
    assert values["screened_at"] is None  # текст сменился — вердикт ИИ устарел


def test_an_unpriced_newer_copy_keeps_the_known_price() -> None:
    primary = listing(1, 0, "https://t.me/a/1")

    values = merge_values(primary, listing(2, 5, "https://t.me/b/2", price=None))

    assert "price_amount" not in values


def test_a_private_link_hands_the_card_to_a_public_copy() -> None:
    private = listing(1, 0, "https://t.me/c/123/1")
    public = listing(2, 5, "https://t.me/b/2")

    assert merge_values(private, public)["tg_link"] == "https://t.me/b/2"


def test_an_identical_copy_changes_nothing_but_freshness() -> None:
    primary = listing(1, 0, "https://t.me/a/1")
    same = replace(primary, posted_at=T0 + timedelta(hours=3))

    assert merge_values(primary, same) == {"posted_at": same.posted_at}


def raw(**origin: object) -> RawMessage:
    return replace(
        RawMessage(chat_tg_id=-1001, msg_id=5, text="t", text_hash="h", posted_at=T0, id=1),
        **origin,  # type: ignore[arg-type]
    )


CHAT = Chat(tg_id=-1001, title="Группа", city="nha_trang", username="group")


def test_a_forward_links_to_its_original() -> None:
    forwarded = raw(origin_kind="forward", origin_msg_id=55, origin_username="Channel")

    assert is_primary(forwarded)
    assert primary_link(forwarded, CHAT) == "https://t.me/Channel/55"
    assert primary_link(raw(), CHAT) == "https://t.me/group/5"


def test_copies_without_a_public_original_are_not_primary() -> None:
    assert not is_primary(raw(origin_kind="forward_hidden"))
    assert not is_primary(raw(origin_kind="pointer", origin_msg_id=3, origin_username="x_chat"))
    # Пересылка с адресом, но без имени канала: ссылку клиенту дать нельзя.
    assert not is_primary(raw(origin_kind="forward", origin_msg_id=55, origin_chat_tg_id=-1009))


def test_a_late_middle_copy_cannot_replace_the_original_after_refresh() -> None:
    primary = listing(1, 0, "https://t.me/a/1")
    repost = listing(2, 48, "https://t.me/b/2", price=5_500_000)
    refreshed = replace(primary, **merge_values(primary, repost))
    middle = listing(3, 24, "https://t.me/middle/3", price=5_800_000)

    assert merge_values(refreshed, middle) == {}
    assert refreshed.primary_posted_at == T0
    assert refreshed.posted_at == repost.posted_at
    assert refreshed.price_amount == Decimal(5_500_000)
