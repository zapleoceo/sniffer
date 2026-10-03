"""Страницы выдачи: порядок без засилья автора, снимок, кнопки, их длина."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sniffer.bot.keyboards import PageCallback, markup, without_paging
from sniffer.bot.paging import (
    SHOW_ALL_CAP,
    MemorySnapshots,
    MoreOffer,
    Snapshot,
    all_label,
    diversify,
    more_label,
    seller_key,
)
from sniffer.bot.presenter import Reply
from sniffer.sources.base import RawItem

T0 = datetime(2026, 10, 4, 12, tzinfo=UTC)


def lot(number: int, *, seller: str = "", seller_id: int | None = None) -> RawItem:
    return RawItem(
        source="archive",
        external_id=str(number),
        url=f"https://t.me/c/1/{number}",
        seller_name=seller,
        raw={} if seller_id is None else {"seller_id": seller_id},
    )


def ids(items: list[RawItem]) -> list[str]:
    return [item.external_id for item in items]


def test_one_seller_takes_at_most_two_places_at_the_front() -> None:
    items = [lot(n, seller="Агентство") for n in range(5)] + [
        lot(n, seller=f"s{n}") for n in (5, 6, 7)
    ]
    front = diversify(items)[:5]
    assert sum(item.seller_name == "Агентство" for item in front) == 2


def test_nothing_is_dropped_only_pushed_back_and_the_rest_keeps_its_order() -> None:
    items = [lot(0, seller="a"), lot(1, seller="a"), lot(2, seller="a"), lot(3), lot(4, seller="b")]
    assert ids(diversify(items)) == ["0", "1", "3", "4", "2"]
    assert sorted(ids(diversify(items))) == sorted(ids(items))


def test_a_seller_without_a_name_is_not_a_group() -> None:
    items = [lot(n) for n in range(6)]
    assert ids(diversify(items)) == ids(items)


def test_the_seller_id_beats_the_name_and_a_flag_is_not_an_id() -> None:
    assert seller_key(lot(1, seller="x", seller_id=7)) == "archive:id:7"
    flagged = RawItem(source="s", external_id="1", url="u", raw={"seller_id": True})
    assert seller_key(flagged) is None


def test_names_differing_in_case_and_spaces_are_one_seller() -> None:
    assert seller_key(lot(1, seller=" Agent  Z ")) == seller_key(lot(2, seller="agent z"))


def snapshot() -> Snapshot:
    return Snapshot(owner=1, items=(lot(1),), root=3)


def test_a_snapshot_is_found_by_its_token_and_expires_after_forty_eight_hours() -> None:
    now = [T0]
    store = MemorySnapshots(clock=lambda: now[0])
    token = store.put(snapshot())
    assert store.get(token) is not None
    now[0] = T0 + timedelta(hours=48, seconds=1)
    assert store.get(token) is None


def test_the_oldest_snapshot_goes_first_when_the_store_is_full() -> None:
    store = MemorySnapshots(capacity=2, clock=lambda: T0)
    first, second, third = (store.put(snapshot()) for _ in range(3))
    assert store.get(first) is None
    assert store.get(second) is not None and store.get(third) is not None


def test_an_unknown_token_is_nothing() -> None:
    assert MemorySnapshots().get("нет") is None


def test_button_labels_say_how_many_and_cap_show_all() -> None:
    assert more_label(5) == "Ещё 5"
    assert all_label(12) == "Показать все 12"
    assert all_label(SHOW_ALL_CAP + 30) == f"Показать {SHOW_ALL_CAP} из {SHOW_ALL_CAP + 30}"


def rows(reply: Reply) -> list[list[InlineKeyboardButton]]:
    keyboard = markup(reply)
    assert keyboard is not None
    return keyboard.inline_keyboard


def test_paging_buttons_come_first_and_all_is_hidden_when_it_equals_more() -> None:
    both = rows(Reply("x", more=MoreOffer("Ab3xYz9Q", 5, 12)))[0]
    assert [button.text for button in both] == ["Ещё 5", "Показать все 12"]
    one = rows(Reply("x", more=MoreOffer("Ab3xYz9Q", 5, 3)))[0]
    assert [button.text for button in one] == ["Ещё 3"]


def test_a_continuation_page_has_only_its_paging_row() -> None:
    assert len(rows(Reply("x", more=MoreOffer("Ab3xYz9Q", 5, 3)))) == 1


def test_callback_data_fits_the_telegram_limit_even_at_the_extremes() -> None:
    longest = PageCallback(token="Ab3xYz9Q" * 2, action="more", offset=99_999).pack()
    assert len(longest.encode()) <= 64
    parsed = PageCallback.unpack(PageCallback(token="Ab3xYz9Q", action="all", offset=5).pack())
    assert (parsed.token, parsed.action, parsed.offset) == ("Ab3xYz9Q", "all", 5)


def test_a_pressed_page_loses_only_its_paging_buttons() -> None:
    reply = Reply("x", offer_subscription=True, more=MoreOffer("Ab3xYz9Q", 5, 12))
    keyboard = markup(reply)
    stripped = without_paging(keyboard)
    assert stripped is not None and keyboard is not None
    before = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    after = [b.callback_data for row in stripped.inline_keyboard for b in row]
    assert len(before) - len(after) == 2
    assert not any((data or "").startswith("pg:") for data in after)
    assert without_paging(None) is None
    empty = InlineKeyboardMarkup(inline_keyboard=[])
    assert without_paging(empty) == empty
