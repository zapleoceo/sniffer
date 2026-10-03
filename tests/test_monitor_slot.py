"""Слот слежения за проход, без базы: потолок суток, «ещё N», дедуп, вердикт, пауза без слота.

Репозитории подменены (`monitor_support`): проверяется оркестрация — что спросили, что
поставили, куда сдвинули курсор. SQL тех же шагов проверяет живая база (`test_db_monitor.py`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from sniffer.domain.monitoring import OVERFLOW_KIND, Overflow, local_day_start
from sniffer.domain.quota import Channel
from sniffer.domain.records import Listing, StoredPassport
from sniffer.worker.monitor import MonitorAgent
from sniffer.worker.monitor_scope import OVERFLOW_DELAY
from tests.monitor_support import NOW, World, install, listing, passport, subscription

# 19:00 по Вьетнаму: не тихие часы, сутки 2026-10-03.
TODAY = date(2026, 10, 3)
# Оплаченный срок: слот со сроком считается в число подписок, слот без срока — нет.
PAID = NOW + timedelta(days=3)


def many(count: int, first: int = 1) -> list[Listing]:
    return [listing(first + index) for index in range(count)]


def queued_ids(world: World) -> list[int]:
    return [item["listing_id"] for item in world.delivery.queued]


class Fixed:
    def __init__(self, slots: int | None) -> None:
        self.slots = slots

    async def count(self, user_id: int, now: datetime) -> int | None:
        return self.slots


async def test_the_day_cap_is_counted_from_vietnamese_midnight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = install(monkeypatch, subscriptions=[subscription()], page=many(1))

    await MonitorAgent().tick(now=NOW)

    assert world.delivery.used_since_args == [local_day_start(NOW)]
    assert local_day_start(NOW) == datetime(2026, 10, 2, 17, 0, tzinfo=UTC)


async def test_under_the_cap_the_newest_go_and_the_rest_become_a_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = install(monkeypatch, subscriptions=[subscription(max_per_day=10)], page=many(15))

    assert await MonitorAgent().tick(now=NOW) == 10

    assert queued_ids(world) == list(range(6, 16)), "новейшие десять, в порядке появления"
    assert world.monitors.overflow == [(1, Overflow(TODAY, 5, True), 5)]


async def test_the_first_overflow_queues_one_summary_after_the_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = install(monkeypatch, subscriptions=[subscription(max_per_day=10)], page=many(13))

    await MonitorAgent().tick(now=NOW)

    [notice] = world.delivery.notices
    assert notice["payload"] == {"kind": OVERFLOW_KIND, "count": 3, "cap": 10}
    assert notice["scheduled_at"] == NOW + OVERFLOW_DELAY
    assert (notice["subscription_id"], notice["user_id"]) == (1, 101)


async def test_a_later_overflow_of_the_day_refines_the_pending_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sub = subscription(max_per_day=10, overflow_day=TODAY, overflow_count=5, overflow_notified=True)
    world = install(monkeypatch, subscriptions=[sub], page=many(12))

    await MonitorAgent().tick(now=NOW)

    assert world.delivery.notices == [], "вторая сводка за сутки — это спам"
    assert world.delivery.bumped == [7]
    assert world.monitors.overflow == [(1, Overflow(TODAY, 7, True), 2)]


async def test_a_new_day_asks_for_a_summary_again(monkeypatch: pytest.MonkeyPatch) -> None:
    yesterday = TODAY - timedelta(days=1)
    sub = subscription(
        max_per_day=10, overflow_day=yesterday, overflow_count=9, overflow_notified=True
    )
    world = install(monkeypatch, subscriptions=[sub], page=many(11))

    await MonitorAgent().tick(now=NOW)

    assert [n["payload"]["count"] for n in world.delivery.notices] == [1]


async def test_nothing_over_the_cap_means_no_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    world = install(monkeypatch, subscriptions=[subscription(max_per_day=10)], page=many(10))

    await MonitorAgent().tick(now=NOW)

    assert world.delivery.notices == [] and world.monitors.overflow == []


async def test_what_was_already_sent_today_shrinks_the_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = install(monkeypatch, subscriptions=[subscription(max_per_day=10)], page=many(5))
    world.delivery.used = 8

    assert await MonitorAgent().tick(now=NOW) == 2

    assert queued_ids(world) == [4, 5]
    assert world.monitors.overflow[0][2] == 3


async def test_the_cursor_moves_past_the_overflow_so_the_old_queue_never_forms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = install(monkeypatch, subscriptions=[subscription(max_per_day=2)], page=many(6))

    await MonitorAgent().tick(now=NOW)

    assert world.delivery.advanced == [(1, 6)]


async def test_a_card_the_client_already_saw_neither_comes_again_nor_takes_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = install(monkeypatch, subscriptions=[subscription(max_per_day=2)], page=many(4))
    world.ledger.already = {4}

    await MonitorAgent().tick(now=NOW)

    assert queued_ids(world) == [2, 3], "четвёртая уже показана, потолок достался остальным"


async def test_delivered_cards_go_to_the_ledger_without_spending_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = install(monkeypatch, subscriptions=[subscription(max_per_day=10)], page=many(3))

    await MonitorAgent().tick(now=NOW)

    [claim] = world.ledger.claims
    assert claim.channel is Channel.MONITOR and not claim.channel.spends_cap
    assert claim.limit is None, "слежение не упирается в потолок 300"
    assert claim.listing_ids == (1, 2, 3)
    assert (claim.user_id, claim.passport_root) == (101, 201)


async def test_a_card_that_was_not_queued_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = install(monkeypatch, subscriptions=[subscription()], page=many(2))

    async def refuse(**_: Any) -> bool:
        return False

    world.delivery.enqueue = refuse  # type: ignore[method-assign]

    await MonitorAgent().tick(now=NOW)

    assert world.ledger.claims == [], "дубль очереди не должен попасть в журнал показов"


async def test_an_old_card_with_a_fresh_id_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = listing(1, posted_at=NOW - timedelta(days=5))
    world = install(monkeypatch, subscriptions=[subscription()], page=[stale])

    assert await MonitorAgent().tick(now=NOW) == 0
    assert world.delivery.queued == []


async def test_the_cursor_waits_in_front_of_a_card_without_a_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = install(monkeypatch, subscriptions=[subscription()], page=many(5))
    world.listings.unready = 4

    assert await MonitorAgent().tick(now=NOW) == 3

    assert world.listings.before_ids == [4], "карточка без вердикта и всё после неё не берутся"
    assert queued_ids(world) == [1, 2, 3]
    assert world.delivery.advanced == [(1, 3)], "курсор встал перед ожидающей, а не за ней"


async def test_without_new_cards_the_slot_does_not_even_ask_for_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = install(monkeypatch, subscriptions=[subscription(scan_listing_id=40)], page=many(3))
    world.listings.head = 40

    assert await MonitorAgent().tick(now=NOW) == 0

    assert world.listings.asked == []
    assert world.monitors.scanned == [1], "слот при этом обойдён"


async def test_slots_beyond_the_paid_count_stand_without_losing_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = subscription(1, expires_at=PAID)
    second = subscription(2, user_id=first.user_id, passport_root=202, expires_at=PAID)
    world = install(monkeypatch, subscriptions=[first, second], page=many(2))
    agent = MonitorAgent(slots=Fixed(1))

    await agent.tick(now=NOW)

    assert world.monitors.scanned == [1], "второй слот не сканируется"
    assert world.monitors.no_slot == {2: NOW}
    assert world.monitors.touched == [2], "но считается обойдённым, иначе стоял бы первым"
    assert world.delivery.advanced == [(1, 2)], "курсор стоящего слота не двигается"
    assert agent.counters.paused_no_slot == 1


async def test_no_slots_at_all_stops_every_monitor(monkeypatch: pytest.MonkeyPatch) -> None:
    paid = [subscription(1, expires_at=PAID), subscription(2, expires_at=PAID)]
    world = install(monkeypatch, subscriptions=paid, page=many(2))

    assert await MonitorAgent(slots=Fixed(0)).tick(now=NOW) == 0

    assert world.monitors.scanned == [] and world.delivery.queued == []
    assert set(world.monitors.no_slot) == {1, 2}


async def test_the_pause_start_is_written_once(monkeypatch: pytest.MonkeyPatch) -> None:
    since = NOW - timedelta(hours=3)
    world = install(
        monkeypatch,
        subscriptions=[subscription(1, expires_at=PAID, no_slot_since=since)],
        page=many(1),
    )

    await MonitorAgent(slots=Fixed(0)).tick(now=NOW)

    assert world.monitors.no_slot == {}, "момент начала паузы не затирается каждым проходом"


async def test_a_long_paused_slot_jumps_to_now_when_it_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_ago = NOW - timedelta(days=3)
    world = install(
        monkeypatch, subscriptions=[subscription(1, no_slot_since=long_ago)], page=many(3)
    )
    world.listings.head = 500

    assert await MonitorAgent().tick(now=NOW) == 0

    assert (1, 500) in world.delivery.advanced
    assert world.monitors.no_slot == {1: None}
    assert world.delivery.queued == [], "накопившееся за паузу не вываливается пачкой"


async def test_a_short_pause_keeps_the_cursor_where_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brief = NOW - timedelta(hours=2)
    world = install(monkeypatch, subscriptions=[subscription(1, no_slot_since=brief)], page=many(3))
    world.listings.head = 500

    assert await MonitorAgent().tick(now=NOW) == 3

    assert (1, 500) not in world.delivery.advanced
    assert world.monitors.no_slot == {1: None}


async def test_a_filter_edit_keeps_the_cursor_of_the_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Правка фильтра — новая версия паспорта той же цепочки: слот читает её, курсор прежний."""
    edited = StoredPassport(id=999, user_id=101, version=2, passport=passport(city="da_nang"))
    world = install(
        monkeypatch, subscriptions=[subscription(1, passport=edited, scan_listing_id=7)], page=[]
    )

    await MonitorAgent().tick(now=NOW)

    [(spec, after_id, _)] = world.listings.asked
    assert spec.city == "da_nang", "применён новый фильтр"
    assert after_id == 7, "курсор прежний"


# ── право слота и возврат после паузы (итоговое ревью wave3, находка 1) ──────────────────


async def test_the_pause_start_is_marked_before_the_portion_is_chosen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = install(monkeypatch, subscriptions=[subscription(1)], page=many(1))

    await MonitorAgent().tick(now=NOW)

    assert world.monitors.marked == [NOW]
    assert world.monitors.order.index("mark_lapsed") < world.monitors.order.index("claim")


async def test_an_unpaid_slot_without_a_term_is_not_stopped_by_a_zero_slot_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Слот владельца (без срока) не занимает оплаченного места и не зависит от него."""
    world = install(monkeypatch, subscriptions=[subscription(1)], page=many(1))

    assert await MonitorAgent(slots=Fixed(0)).tick(now=NOW) == 1

    assert world.monitors.scanned == [1] and world.monitors.no_slot == {}


async def test_a_paid_slot_beyond_the_live_subscriptions_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    term = PAID
    first = subscription(1, expires_at=term)
    second = subscription(2, user_id=first.user_id, passport_root=202, expires_at=term)
    world = install(monkeypatch, subscriptions=[first, second], page=many(1))

    await MonitorAgent(slots=Fixed(1)).tick(now=NOW)

    assert world.monitors.scanned == [1] and world.monitors.no_slot == {2: NOW}


async def test_a_slot_back_after_a_long_gap_starts_from_now_not_from_the_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Продление после истечения: пауза записана `mark_lapsed` (>24 ч), курсор прыгает."""
    gap = NOW - timedelta(days=5)
    renewed = subscription(1, expires_at=NOW + timedelta(days=30), no_slot_since=gap)
    world = install(monkeypatch, subscriptions=[renewed], page=many(3))
    world.listings.head = 900

    assert await MonitorAgent(slots=Fixed(1)).tick(now=NOW) == 0

    assert (1, 900) in world.delivery.advanced and world.monitors.no_slot == {1: None}
