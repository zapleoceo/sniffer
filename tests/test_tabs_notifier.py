"""Нотифаер и темы: карточка идёт в тему своего поиска, при потере темы — в General один раз."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from sniffer.notifier.delivery import LOST_NOTE, Delivery
from sniffer.notifier.ports import Work
from tests.notifier_support import Clock, Row, Store, Txn, digest_row

GONE = "Bad Request: message thread not found"


class FakeTabs:
    """Живые темы подписок и утраченные связи."""

    def __init__(self, threads: dict[int, int]) -> None:
        self.threads = dict(threads)
        self.lost: list[int] = []

    async def threads_for(self, subscription_ids: Sequence[int]) -> dict[int, int]:
        return {s: t for s, t in self.threads.items() if s in subscription_ids}

    async def mark_lost(self, subscription_id: int) -> bool:
        self.lost.append(subscription_id)
        self.threads.pop(subscription_id, None)
        return True


class Wire:
    """Что ушло в Telegram: `(получатель, текст, тема)`; тема `None` — без темы."""

    def __init__(self, *, thread_error: BaseException | None = None) -> None:
        self.sent: list[tuple[int, str, int | None]] = []
        self.thread_error = thread_error

    async def plain(self, user_id: int, text: str) -> None:
        self.sent.append((user_id, text, None))

    async def in_thread(self, user_id: int, text: str, thread_id: int) -> None:
        if self.thread_error is not None:
            raise self.thread_error
        self.sent.append((user_id, text, thread_id))


def build(
    rows: list[Row], tabs: FakeTabs | None, wire: Wire, *, with_thread_sender: bool = True
) -> tuple[Delivery, Store]:
    store = Store(rows)

    @asynccontextmanager
    async def scope() -> AsyncIterator[Work]:
        unit = Txn(store)
        try:
            yield Work(queue=unit, users=unit, commit=unit.commit, tabs=tabs)
        finally:
            unit.close()

    delivery = Delivery(
        wire.plain,
        send_in_thread=wire.in_thread if with_thread_sender else None,
        clock=Clock(),
        scope=scope,
        pause_s=0,
    )
    return delivery, store


def row(identifier: int, subscription: int | None, **extra: Any) -> Row:
    return Row(identifier, subscription_id=subscription, **extra)


async def test_a_card_goes_to_the_thread_of_its_search() -> None:
    wire = Wire()
    delivery, store = build([row(1, 10)], FakeTabs({10: 555}), wire)

    assert await delivery.tick() == 1
    assert [(r, t) for r, _, t in wire.sent] == [(42, 555)]
    assert store.row(1).status == "sent"


async def test_a_search_without_a_tab_gets_no_thread() -> None:
    wire = Wire()
    delivery, _ = build([row(1, 11)], FakeTabs({10: 555}), wire)
    await delivery.tick()
    assert [t for _, _, t in wire.sent] == [None]


async def test_without_topics_the_sender_is_the_old_one_and_threads_are_never_asked() -> None:
    wire = Wire()
    delivery, _ = build([row(1, 10)], None, wire)
    await delivery.tick()
    assert [t for _, _, t in wire.sent] == [None]


async def test_without_a_thread_sender_a_known_thread_is_ignored_not_crashing() -> None:
    wire = Wire()
    delivery, _ = build([row(1, 10)], FakeTabs({10: 555}), wire, with_thread_sender=False)
    await delivery.tick()
    assert [t for _, _, t in wire.sent] == [None]


async def test_a_message_without_a_subscription_never_looks_for_a_thread() -> None:
    wire = Wire()
    delivery, _ = build([row(1, None)], FakeTabs({10: 555}), wire)
    await delivery.tick()
    assert [t for _, _, t in wire.sent] == [None]


async def test_digests_of_two_searches_are_not_glued_together() -> None:
    wire = Wire()
    rows = [
        digest_row(1, user_id=7),
        digest_row(2, user_id=7),
        digest_row(3, user_id=7),
        digest_row(4, user_id=7),
    ]
    for r, sub in zip(rows, (10, 10, 20, 20), strict=True):
        r.subscription_id = sub
    delivery, _ = build(rows, FakeTabs({10: 555, 20: 666}), wire)

    assert await delivery.tick() == 4
    assert sorted(t for _, _, t in wire.sent if t is not None) == [555, 666]
    assert len(wire.sent) == 2
    first = next(text for _, text, t in wire.sent if t == 555)
    assert "Карточка 1" in first and "Карточка 2" in first and "Карточка 3" not in first


async def test_a_deleted_topic_falls_back_to_general_once_and_marks_the_link_lost() -> None:
    gone = TelegramBadRequest(SendMessage(chat_id=1, text="x"), GONE)
    wire, tabs = Wire(thread_error=gone), FakeTabs({10: 555})
    delivery, store = build([row(1, 10)], tabs, wire)

    assert await delivery.tick() == 1

    assert tabs.lost == [10]
    ((_, text, thread),) = wire.sent
    assert thread is None and LOST_NOTE.split(".")[0] in text
    assert store.row(1).status == "sent"


async def test_the_next_card_after_a_lost_topic_has_no_note_and_no_thread() -> None:
    gone = TelegramBadRequest(SendMessage(chat_id=1, text="x"), GONE)
    wire, tabs = Wire(thread_error=gone), FakeTabs({10: 555})
    delivery, _ = build([row(1, 10), row(2, 10)], tabs, wire)

    await delivery.tick()
    await delivery.tick()

    assert [t for _, _, t in wire.sent] == [None, None]
    assert LOST_NOTE.split(".")[0] in wire.sent[0][1]
    assert LOST_NOTE.split(".")[0] not in wire.sent[1][1]


async def test_another_bad_request_in_a_thread_is_not_treated_as_a_lost_topic() -> None:
    other = TelegramBadRequest(SendMessage(chat_id=1, text="x"), "Bad Request: message is too long")
    wire, tabs = Wire(thread_error=other), FakeTabs({10: 555})
    delivery, store = build([row(1, 10)], tabs, wire)

    await delivery.tick()

    assert tabs.lost == [] and wire.sent == []
    assert store.row(1).status == "failed"


async def test_a_blocked_bot_is_still_a_block_in_a_thread() -> None:
    from aiogram.exceptions import TelegramForbiddenError

    forbidden = TelegramForbiddenError(
        SendMessage(chat_id=1, text="x"), "Forbidden: bot was blocked"
    )
    wire, tabs = Wire(thread_error=forbidden), FakeTabs({10: 555})
    delivery, store = build([row(1, 10)], tabs, wire)

    await delivery.tick()

    assert tabs.lost == []
    assert store.row(1).status == "cancelled"


async def test_the_unknown_failure_in_a_thread_does_not_lose_the_card() -> None:
    wire, tabs = Wire(thread_error=OSError("net")), FakeTabs({10: 555})
    delivery, store = build([row(1, 10)], tabs, wire)

    await delivery.tick()

    assert tabs.lost == []
    assert store.row(1).status == "pending" and store.row(1).attempts == 1


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, BaseExceptionGroup])
async def test_an_interrupt_inside_a_thread_send_still_propagates(interrupt: type) -> None:
    error = (
        interrupt()
        if interrupt is KeyboardInterrupt
        else BaseExceptionGroup("g", [KeyboardInterrupt()])
    )
    wire, tabs = Wire(thread_error=error), FakeTabs({10: 555})
    delivery, _ = build([row(1, 10)], tabs, wire)
    with pytest.raises((KeyboardInterrupt, BaseExceptionGroup)):
        await delivery.tick()
