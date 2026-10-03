"""«Ещё N» и «Показать все N»: те же ворота квоты, курсор, возврат при сбое."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from sniffer.bot import more_cards, wording_plan
from sniffer.bot.cards import visible_len
from sniffer.bot.paging import SHOW_ALL_CAP, MemorySnapshots, Snapshot
from sniffer.bot.presenter import Reply
from sniffer.bot.quota import Account, QuotaService
from sniffer.bot.showing import show
from sniffer.domain.plans import FREE_CARDS_PER_PERIOD, PAID_CARDS_PER_PERIOD
from sniffer.search.intake_rules import parse_query
from sniffer.simulation.ledger import MemoryLedger
from sniffer.sources.base import RawItem
from tests.quota_support import Clock, Slots

T0 = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)
FRESH = datetime.now(UTC) - timedelta(days=1)
ME = Account(user_id=1, tg_user_id=1001)
PASSPORT = parse_query("скутер в Нячанге")
ROOT = 7


pytestmark = pytest.mark.usefixtures("sales_on")


@dataclass
class Result:
    items: list[RawItem] = field(default_factory=list)
    status: str | None = None
    deferred: bool = False
    capped: bool = False


class Sent:
    def __init__(self, *, fail: bool = False) -> None:
        self.replies: list[Reply] = []
        self.fail = fail

    async def __call__(self, reply: Reply) -> None:
        if self.fail:
            raise ConnectionError("telegram недоступен")
        self.replies.append(reply)

    @property
    def text(self) -> str:
        return "\n".join(reply.text for reply in self.replies)

    @property
    def cards(self) -> int:
        return self.text.count("открыть оригинал")


def lot(number: int, *, seller: str = "") -> RawItem:
    return RawItem(
        source="archive",
        external_id=str(number),
        url=f"https://t.me/c/1/{number}",
        title=f"Honda Vision {number}",
        price_raw="25 млн",
        seller_name=seller,
        posted_at=FRESH,
        raw={"listing_id": number},
    )


class Shop:
    """Квота на подделке журнала и хранилище снимков: всё, что нужно показу и «Ещё»."""

    def __init__(self, *, slots: int = 0, owner: bool = False) -> None:
        self.ledger = MemoryLedger()
        self.quota = QuotaService(
            self.ledger,
            entitlements=Slots(slots),
            clock=Clock(T0),
            owner_tg_id=ME.tg_user_id if owner else None,
        )
        self.snapshots = MemorySnapshots(clock=lambda: T0)

    async def first(self, items: list[RawItem], send: Sent | None = None) -> Sent:
        sent = send or Sent()
        await show(
            sent,
            PASSPORT,
            Result(items),
            root=ROOT,
            quota=self.quota,
            account=ME,
            snapshots=self.snapshots,
        )
        return sent

    def token(self, sent: Sent) -> str:
        offer = sent.replies[-1].more
        assert offer is not None, "под страницей нет кнопки продолжения"
        return offer.token

    async def press(
        self, token: str, action: str, offset: int, sent: Sent | None = None
    ) -> tuple[more_cards.Result, Sent]:
        out = sent or Sent()
        snapshot = self.snapshots.get(token)
        assert snapshot is not None
        outcome = await more_cards.show_more(
            out, token, snapshot, action, offset, quota=self.quota, account=ME
        )
        return outcome, out

    async def used(self) -> int:
        return (await self.quota.standing(ME)).used


async def test_a_long_result_offers_more_and_more_shows_the_next_five() -> None:
    shop = Shop(slots=1)
    sent = await shop.first([lot(n) for n in range(12)])
    assert sent.replies[-1].more is not None
    assert sent.replies[-1].more.rest == 7
    assert sent.cards == 5
    outcome, page = await shop.press(shop.token(sent), "more", 5)
    assert outcome is more_cards.Result.SHOWN
    assert page.cards == 5
    assert "Карточки 6–10 из 12" in page.text
    assert page.replies[-1].more is not None and page.replies[-1].more.rest == 2


async def test_show_all_shows_the_whole_rest_in_one_pass_and_the_next_press_is_stale() -> None:
    shop = Shop(slots=1)
    sent = await shop.first([lot(n) for n in range(12)])
    token = shop.token(sent)
    outcome, page = await shop.press(token, "all", 5)
    assert outcome is more_cards.Result.SHOWN and page.cards == 7
    assert page.replies[-1].more is None
    again, silent = await shop.press(token, "all", 5)
    assert again is more_cards.Result.STALE and silent.replies == []


async def test_a_double_tap_shows_the_page_once_and_charges_once() -> None:
    shop = Shop(slots=1)
    token = shop.token(await shop.first([lot(n) for n in range(12)]))
    first, second = await shop.press(token, "more", 5), await shop.press(token, "more", 5)
    assert (first[0], second[0]) == (more_cards.Result.SHOWN, more_cards.Result.STALE)
    assert await shop.used() == 10


async def test_an_old_button_with_a_wrong_offset_shows_nothing() -> None:
    shop = Shop(slots=1)
    token = shop.token(await shop.first([lot(n) for n in range(12)]))
    outcome, sent = await shop.press(token, "more", 0)
    assert outcome is more_cards.Result.STALE and sent.replies == []


async def test_a_card_shown_before_is_not_charged_again_on_the_next_page() -> None:
    shop = Shop(slots=0)
    await shop.first([lot(n) for n in range(3)])  # три карточки уже у клиента
    sent = await shop.first([lot(n) for n in range(8)])  # поиск снова: те же три и пять новых
    assert await shop.used() == 5, "три прежних не списаны повторно, только две новых"
    token = shop.token(sent)
    outcome, page = await shop.press(token, "more", 5)
    assert outcome is more_cards.Result.SHOWN and page.cards == 3
    assert await shop.used() == 8


async def test_the_free_limit_ends_the_paging_and_the_rest_is_counted_honestly() -> None:
    shop = Shop(slots=0)
    sent = await shop.first([lot(n) for n in range(14)])
    outcome, page = await shop.press(shop.token(sent), "more", 5)
    assert outcome is more_cards.Result.SHOWN and page.cards == 5
    assert page.replies[-1].more is None, "лимит исчерпан: кнопки «Ещё» быть не должно"
    assert "Ещё 4 подходящих варианта" in page.text and "подписке" in page.text


async def test_the_limit_cuts_a_show_all_and_the_cut_part_is_not_shown_or_charged() -> None:
    shop = Shop(slots=0)
    sent = await shop.first([lot(n) for n in range(14)])
    outcome, page = await shop.press(shop.token(sent), "all", 5)
    assert outcome is more_cards.Result.SHOWN and page.cards == 5
    assert await shop.used() == FREE_CARDS_PER_PERIOD


async def test_a_paid_page_of_show_all_is_capped_and_paging_goes_on() -> None:
    shop = Shop(slots=1)
    items = [lot(n) for n in range(PAID_CARDS_PER_PERIOD + 20)]
    sent = await shop.first(items)
    outcome, page = await shop.press(shop.token(sent), "all", 5)
    assert outcome is more_cards.Result.SHOWN and page.cards == SHOW_ALL_CAP
    assert page.replies[-1].more is not None, "до потолка периода «Ещё» остаётся"


async def test_the_owner_has_no_limit_and_keeps_getting_buttons() -> None:
    shop = Shop(owner=True)
    sent = await shop.first([lot(n) for n in range(30)])
    outcome, page = await shop.press(shop.token(sent), "more", 5)
    assert outcome is more_cards.Result.SHOWN and page.replies[-1].more is not None


async def test_a_failed_send_gives_the_slots_back_and_the_press_can_be_repeated() -> None:
    shop = Shop(slots=1)
    token = shop.token(await shop.first([lot(n) for n in range(12)]))
    before = await shop.used()
    with pytest.raises(ConnectionError):
        await shop.press(token, "more", 5, Sent(fail=True))
    assert await shop.used() == before
    outcome, page = await shop.press(token, "more", 5)
    assert outcome is more_cards.Result.SHOWN and page.cards == 5


async def test_a_dead_quota_says_so_and_leaves_the_cursor_where_it_was() -> None:
    shop = Shop(slots=1)
    token = shop.token(await shop.first([lot(n) for n in range(12)]))

    async def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("база недоступна")

    cast(Any, shop.quota).admit = broken
    outcome, page = await shop.press(token, "more", 5)
    assert outcome is more_cards.Result.FAILED
    assert page.text == wording_plan.QUOTA_UNAVAILABLE
    snapshot = shop.snapshots.get(token)
    assert snapshot is not None and snapshot.cursor == 5


async def test_show_all_splits_into_messages_that_fit_telegram() -> None:
    shop = Shop(slots=1)
    items = [lot(n) for n in range(60)]
    for item in items:
        item.title = "Очень длинное название лота " * 4
    sent = await shop.first(items)
    outcome, page = await shop.press(shop.token(sent), "all", 5)
    assert outcome is more_cards.Result.SHOWN
    assert page.cards == SHOW_ALL_CAP
    assert len(page.replies) > 1
    assert all(visible_len(reply.text) < 4096 for reply in page.replies)
    assert all(reply.more is None for reply in page.replies[:-1])


async def test_a_short_result_has_no_buttons() -> None:
    shop = Shop(slots=1)
    sent = await shop.first([lot(n) for n in range(5)])
    assert sent.replies[-1].more is None


async def test_a_page_with_one_more_card_offers_one_card() -> None:
    shop = Shop(slots=1)
    sent = await shop.first([lot(n) for n in range(6)])
    offer = sent.replies[-1].more
    assert offer is not None and offer.rest == 1


async def test_the_first_page_does_not_let_one_seller_fill_it() -> None:
    shop = Shop(slots=1)
    items = [lot(n, seller="Агентство") for n in range(5)] + [lot(n) for n in range(5, 9)]
    sent = await shop.first(items)
    assert sent.text.count("Honda Vision") == 5
    assert sum(f"Vision {n}" in sent.text for n in range(5)) == 2


async def test_without_a_quota_the_page_is_plain_and_unpaged() -> None:
    sent = Sent()
    await show(sent, PASSPORT, Result([lot(n) for n in range(12)]), root=ROOT)
    assert all(reply.more is None for reply in sent.replies)


async def test_a_snapshot_keeps_the_order_the_client_saw() -> None:
    shop = Shop(slots=1)
    items = [lot(n, seller="a") for n in range(4)] + [lot(n, seller="b") for n in range(4, 8)]
    token = shop.token(await shop.first(items))
    snapshot: Snapshot | None = shop.snapshots.get(token)
    assert snapshot is not None
    assert [item.external_id for item in snapshot.items][:4] == ["0", "1", "4", "5"]


class FailsAfter(Sent):
    """Первые `ok` сообщений уходят, следующее — отказ Telegram."""

    def __init__(self, ok: int) -> None:
        super().__init__()
        self.ok = ok

    async def __call__(self, reply: Reply) -> None:
        if len(self.replies) >= self.ok:
            raise ConnectionError("telegram недоступен")
        self.replies.append(reply)


def long_lots(count: int) -> list[RawItem]:
    items = [lot(n) for n in range(count)]
    for item in items:
        item.title = "Очень длинное название лота " * 4
    return items


async def test_a_half_delivered_page_releases_only_the_undelivered_cards() -> None:
    """Карточки уже ушедшего сообщения клиент видел — списание остаётся, остальное возвращается."""
    shop = Shop(slots=1)
    token = shop.token(await shop.first(long_lots(60)))
    before = await shop.used()
    out = FailsAfter(1)
    with pytest.raises(ConnectionError):
        await shop.press(token, "all", 5, out)
    delivered = out.cards
    assert 0 < delivered < SHOW_ALL_CAP
    assert await shop.used() == before + delivered
    snapshot = shop.snapshots.get(token)
    assert snapshot is not None and snapshot.cursor == 5, (
        "курсор вернулся: страницу можно повторить"
    )


async def test_a_presenter_failure_gives_the_reserved_slots_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shop = Shop(slots=1)
    token = shop.token(await shop.first([lot(n) for n in range(12)]))
    before = await shop.used()

    def boom(*_args: object, **_kwargs: object) -> list[Reply]:
        raise RuntimeError("презентер упал")

    monkeypatch.setattr(more_cards, "present_page", boom)
    with pytest.raises(RuntimeError):
        await shop.press(token, "more", 5)
    assert await shop.used() == before


async def test_the_cursor_stops_in_front_of_the_cards_the_quota_held_back() -> None:
    shop = Shop(slots=0)
    token = shop.token(await shop.first([lot(n) for n in range(14)]))
    outcome, page = await shop.press(token, "all", 5)
    assert outcome is more_cards.Result.SHOWN and page.cards == 5
    snapshot = shop.snapshots.get(token)
    assert snapshot is not None
    assert snapshot.cursor == FREE_CARDS_PER_PERIOD, (
        "курсор на первой удержанной, а не в конце снимка"
    )
