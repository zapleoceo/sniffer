"""Доставка из очереди: троттлинг, повтор, отказ — и экранирование чужого текста."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from sniffer.domain.records import OutboxMessage
from sniffer.notifier.delivery import MAX_ATTEMPTS, Delivery, render

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

PAYLOAD = {
    "listing_id": 1,
    "title": "Honda Vision 2021",
    "summary": "Автомат, документы есть",
    "url": "https://t.me/c/1/1",
    "price_amount": "15000000",
    "price_currency": "VND",
    "posted_at": NOW.isoformat(),
}


@dataclass
class FakeRepo:
    """Очередь без базы: помнит, что доставка с ней сделала."""

    pending: list[OutboxMessage] = field(default_factory=list)
    sent: list[int] = field(default_factory=list)
    retried: list[tuple[int, datetime]] = field(default_factory=list)
    dropped: list[int] = field(default_factory=list)

    async def take_pending(self, *, limit: int, now: datetime) -> list[OutboxMessage]:
        return self.pending[:limit]

    async def mark_sent(self, message_id: int, *, now: datetime) -> None:
        self.sent.append(message_id)

    async def mark_failed(self, message_id: int, *, retry_at: datetime) -> None:
        self.retried.append((message_id, retry_at))

    async def give_up(self, message_id: int) -> None:
        self.dropped.append(message_id)


def message(identifier: int = 1, *, attempts: int = 0) -> OutboxMessage:
    return OutboxMessage(id=identifier, user_id=42, payload=PAYLOAD, attempts=attempts)


async def deliver(repo: FakeRepo, send: object, monkeypatch: pytest.MonkeyPatch) -> int:
    """Доставка с подменённой сессией: проверяем поведение, а не SQL."""
    from sniffer.notifier import delivery as module

    class Session:
        async def commit(self) -> None:
            return None

    class Scope:
        async def __aenter__(self) -> Session:
            return Session()

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(module, "session_scope", lambda: Scope())
    monkeypatch.setattr(module, "DeliveryRepository", lambda _session: repo)
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    return await Delivery(send, pause_s=0.0).tick(now=NOW)  # type: ignore[arg-type]


async def _no_sleep(_seconds: float) -> None:
    return None


async def test_a_delivered_message_leaves_the_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[int, str]] = []

    async def send(user_id: int, text: str) -> None:
        seen.append((user_id, text))

    repo = FakeRepo(pending=[message()])

    assert await deliver(repo, send, monkeypatch) == 1
    assert repo.sent == [1] and not repo.retried
    assert seen[0][0] == 42 and "Honda Vision 2021" in seen[0][1]


async def test_a_failed_send_comes_back_later_instead_of_vanishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Недоступный Telegram — причина подождать, а не выбросить карточку."""

    async def boom(_user_id: int, _text: str) -> None:
        raise ConnectionError("сеть отвалилась")

    repo = FakeRepo(pending=[message()])

    assert await deliver(repo, boom, monkeypatch) == 0
    assert repo.retried and repo.retried[0][1] > NOW
    assert not repo.sent and not repo.dropped


async def test_after_the_last_attempt_the_message_is_given_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Три причины подряд означают, что клиент заблокировал бота."""

    async def boom(_user_id: int, _text: str) -> None:
        raise RuntimeError("bot was blocked by the user")

    repo = FakeRepo(pending=[message(attempts=MAX_ATTEMPTS - 1)])

    await deliver(repo, boom, monkeypatch)

    assert repo.dropped == [1] and not repo.retried


async def test_one_broken_message_does_not_stop_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проход обязан дойти до конца очереди, а не встать на первой ошибке."""

    async def send(user_id: int, _text: str) -> None:
        if user_id == 0:
            raise ValueError("нельзя")

    broken = OutboxMessage(id=1, user_id=0, payload=PAYLOAD)
    repo = FakeRepo(pending=[broken, message(2)])

    assert await deliver(repo, send, monkeypatch) == 1
    assert repo.sent == [2]


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
