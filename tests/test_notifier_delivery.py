"""Доставка из очереди: транзакция на сообщение, честный `sent_at`, повтор и отказ.

Очередь здесь — `tests/notifier_support.py`: таблица в памяти с настоящей границей
транзакции (чего не закоммитили, того «в базе» нет). Подмена честна ровно в этом, и
именно поэтому тесты ловят прежний один-коммит-на-пачку: на нём первое сообщение
после аварии исчезало бы вместе со всем остальным.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.notifier.delivery import MAX_ATTEMPTS, RETRY_AFTER, Delivery, render
from sniffer.notifier.ports import Queue
from tests.notifier_support import (
    PAYLOAD,
    START,
    Boom,
    Clock,
    Row,
    Store,
    Telegram,
    Txn,
    digest_row,
)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пауза между сообщениями в тестах не нужна, а ждать её по-настоящему нельзя."""

    async def instantly(_seconds: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", instantly)


def deliver(
    store: Store, clock: Clock, *outcomes: BaseException | None
) -> tuple[Delivery, Telegram]:
    telegram = Telegram(store, clock, *outcomes)
    return Delivery(telegram, pause_s=0.0, clock=clock, scope=store.scope), telegram


async def test_a_delivered_message_leaves_the_queue() -> None:
    store, clock = Store([Row(1)]), Clock()
    delivery, telegram = deliver(store, clock)

    assert await delivery.tick() == 1

    assert store.row(1).status == "sent" and store.row(1).attempts == 0
    assert telegram.recipients == [42] and "Honda Vision 2021" in telegram.texts[0]


async def test_a_failed_send_comes_back_later_instead_of_vanishing() -> None:
    """Недоступный Telegram — причина подождать, а не выбросить карточку."""
    store, clock = Store([Row(1)]), Clock()
    delivery, _ = deliver(store, clock, ConnectionError("сеть отвалилась"))

    assert await delivery.tick() == 0

    row = store.row(1)
    assert row.status == "pending" and row.attempts == 1
    assert row.scheduled_at == START + RETRY_AFTER


async def test_after_the_last_attempt_the_message_is_given_up() -> None:
    """Попытки конечны: бесконечный повтор — это тот же спам, только позже."""
    store, clock = Store([Row(1, attempts=MAX_ATTEMPTS - 1)]), Clock()
    delivery, _ = deliver(store, clock, RuntimeError("не доставить"))

    await delivery.tick()

    assert store.row(1).status == "failed"


async def test_one_broken_message_does_not_stop_the_rest() -> None:
    """Проход обязан дойти до конца очереди, а не встать на первой ошибке."""
    store = Store([Row(1, recipient_id=0), Row(2)])
    clock = Clock()
    delivery, _ = deliver(store, clock, ValueError("нельзя"))

    assert await delivery.tick() == 1

    assert (store.row(1).status, store.row(2).status) == ("pending", "sent")


async def test_digest_cards_are_sent_as_one_message() -> None:
    store = Store([digest_row(1, title="First"), digest_row(2, title="Second")])
    clock = Clock()
    delivery, telegram = deliver(store, clock)

    assert await delivery.tick() == 2

    assert len(telegram.texts) == 1
    assert "First" in telegram.texts[0] and "Second" in telegram.texts[0]
    assert (store.row(1).status, store.row(2).status) == ("sent", "sent")


# ── транзакция на сообщение ─────────────────────────────────────────────────


async def test_every_message_is_committed_before_the_next_one_is_sent() -> None:
    """Прежний коммит стоял в конце прохода: убитый процесс забывал всю пачку."""
    store, clock = Store([Row(1), Row(2, user_id=8, recipient_id=43)]), Clock()
    delivery, _ = deliver(store, clock)

    await delivery.tick()

    assert store.events == [
        "lock:1",
        "send:42",
        "mark_sent:1",
        "commit",
        "lock:2",
        "send:43",
        "mark_sent:2",
        "commit",
    ]


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), asyncio.CancelledError()])
async def test_a_crash_between_two_messages_keeps_the_first_one_sent(
    interrupt: BaseException,
) -> None:
    """Процесс убили на втором сообщении: первое уже ушло и обязано остаться помеченным."""
    store = Store([Row(1), Row(2, user_id=8, recipient_id=43)])
    clock = Clock()
    delivery, _ = deliver(store, clock, None, interrupt)

    with pytest.raises(type(interrupt)):
        await delivery.tick()

    assert store.row(1).status == "sent", "отправленное потеряно: коммит был один на проход"
    assert store.row(2).status == "pending" and store.row(2).attempts == 0


async def test_a_failure_of_one_message_does_not_roll_back_the_ones_already_sent() -> None:
    store = Store([Row(1), Row(2, user_id=8, recipient_id=43), Row(3, user_id=9, recipient_id=44)])
    clock = Clock()
    delivery, _ = deliver(store, clock, None, Boom("сбой на втором"))

    assert await delivery.tick() == 2

    assert [store.row(i).status for i in (1, 2, 3)] == ["sent", "pending", "sent"]
    assert "rollback" not in store.events


async def test_sent_at_is_the_moment_telegram_confirmed_not_the_start_of_the_pass() -> None:
    """За проход из нескольких сообщений набегают секунды: метка у каждого своя."""
    rows = [Row(1), Row(2, user_id=8, recipient_id=43), Row(3, user_id=9, recipient_id=44)]
    store, clock = Store(rows), Clock()
    delivery, _ = deliver(store, clock)

    await delivery.tick()

    stamps = [store.row(i).sent_at for i in (1, 2, 3)]
    assert stamps == [START + timedelta(seconds=n) for n in (1, 2, 3)]


async def test_every_card_of_a_digest_gets_the_moment_of_its_confirmation() -> None:
    store = Store([digest_row(1), digest_row(2)])
    clock = Clock()
    delivery, _ = deliver(store, clock)

    await delivery.tick()

    assert store.row(1).sent_at == store.row(2).sent_at == START + timedelta(seconds=1)


async def test_a_row_taken_by_another_copy_is_not_sent_and_costs_no_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Вторая копия нотифаера берёт строку первой: слать второй раз нельзя."""
    store = Store([Row(1), Row(2, user_id=8, recipient_id=43)])
    store.locked_elsewhere = {1}
    pauses: list[float] = []

    async def record(seconds: float) -> None:
        pauses.append(seconds)

    monkeypatch.setattr("asyncio.sleep", record)
    clock = Clock()
    delivery, telegram = deliver(store, clock)

    assert await delivery.tick() == 1

    assert telegram.recipients == [43] and store.row(1).status == "pending"
    assert pauses == [], "пауза после строки, к Telegram не ходившей, — потерянное время"


async def test_a_row_sent_by_another_copy_after_planning_is_skipped() -> None:
    store = Store([Row(1)])

    def other_copy_sends_first(shared: Store) -> None:
        shared.rows[1].status = "sent"

    store.before_lock = other_copy_sends_first
    clock = Clock()
    delivery, telegram = deliver(store, clock)

    assert await delivery.tick() == 0
    assert telegram.texts == []


async def test_a_pause_separates_two_requests_to_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    pauses: list[float] = []

    async def record(seconds: float) -> None:
        pauses.append(seconds)

    monkeypatch.setattr("asyncio.sleep", record)
    store = Store([Row(1), Row(2, user_id=8, recipient_id=43), Row(3, user_id=9, recipient_id=44)])
    clock = Clock()
    telegram = Telegram(store, clock)

    await Delivery(telegram, pause_s=2.5, clock=clock, scope=store.scope).tick()

    assert pauses == [2.5, 2.5], "между тремя сообщениями две паузы, не три и не ноль"


# ── разметка ────────────────────────────────────────────────────────────────


def test_hostile_text_from_a_stranger_never_becomes_markup() -> None:
    """Текст объявления писал незнакомый человек, а Bot API принимает HTML."""
    nasty = '<script>alert("x")</script>'
    card = render({**PAYLOAD, "title": nasty, "summary": nasty})

    assert "<script>" not in card
    assert "&lt;script&gt;" in card


def test_a_listing_without_a_price_says_so_plainly() -> None:
    assert "цена не указана" in render({**PAYLOAD, "price_amount": ""})


def test_the_price_is_readable() -> None:
    assert "15 000 000 VND" in render(PAYLOAD)


def test_collection_result_is_structured_and_escapes_every_field() -> None:
    result = render(
        {
            "kind": "collection_result",
            "intro": "Нашлось <одно>",
            "items": [{**PAYLOAD, "title": "<Honda>", "price_display": "7 < 8 млн"}],
        }
    )

    assert "Нашлось &lt;одно&gt;" in result
    assert "<Honda>" not in result and "&lt;Honda&gt;" in result
    assert "7 &lt; 8 млн" in result


# ── подмена очереди не врёт про настоящую ───────────────────────────────────


def _queue_methods() -> list[str]:
    return [name for name, member in vars(Queue).items() if inspect.iscoroutinefunction(member)]


def _shape(function: Callable[..., Any]) -> list[tuple[str, object]]:
    parameters = inspect.signature(function).parameters.values()
    return [(p.name, p.kind) for p in parameters if p.name != "self"]


def test_the_fake_queue_has_the_same_methods_and_signatures_as_the_real_one() -> None:
    """Заглушка, принимающая что угодно, делает тест зелёным при любой ошибке вызова.

    Правило 5 из CLAUDE.md: ловится тем же способом, что и всё остальное — сверкой
    с настоящим. Репозиторий и подмена обязаны отвечать на одни и те же вызовы.
    """
    assert _queue_methods(), "у протокола нет методов: сверять нечего"
    for name in _queue_methods():
        assert _shape(getattr(Txn, name)) == _shape(getattr(DeliveryRepository, name)), name
