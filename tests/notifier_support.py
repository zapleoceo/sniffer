"""Общие заготовки тестов нотифаера: очередь в памяти с настоящей границей транзакции.

Отдельным файлом, а не копией в каждом: два модуля тестов (поведение доставки и
структурная охрана исходов) собирают одну и ту же очередь, и две копии разошлись
бы на первой правке репозитория.

Подмена честная ровно в одном: чего не закоммитили, того в «базе» нет. Без этого
тест «сбой одной отправки не откатывает остальные» зеленел бы и на старом коде с
одним коммитом в конце прохода — записи там были бы видны сразу.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sniffer.domain.records import OutboxMessage
from sniffer.notifier.delivery import Delivery
from sniffer.notifier.policy import Policy
from sniffer.notifier.ports import Work

START = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)

PAYLOAD: dict[str, Any] = {
    "listing_id": 1,
    "title": "Honda Vision 2021",
    "summary": "Автомат, документы есть",
    "url": "https://t.me/c/1/1",
    "price_amount": "15000000",
    "price_currency": "VND",
}


class Boom(Exception):
    """Чужой тип: код о нём не знает и знать не должен, ни в одном списке `except` его нет."""


class Clock:
    """Часы теста: время идёт только тогда, когда его двигают."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@dataclass
class Row:
    id: int
    user_id: int = 7
    recipient_id: int = 42
    payload: dict[str, Any] = field(default_factory=lambda: dict(PAYLOAD))
    attempts: int = 0
    scheduled_at: datetime = START
    status: str = "pending"
    sent_at: datetime | None = None
    last_error: str | None = None
    subscription_id: int | None = None


def tg_id(user_id: int) -> int:
    """Telegram id клиента в тестах: 7 → 42, 8 → 43. Внутренний id и адрес — разные числа."""
    return user_id + 35


def digest_row(identifier: int, *, user_id: int = 7, **payload: Any) -> Row:
    body = {**PAYLOAD, "delivery_mode": "digest", "title": f"Карточка {identifier}", **payload}
    return Row(id=identifier, user_id=user_id, recipient_id=tg_id(user_id), payload=body)


def _message(row: Row) -> OutboxMessage:
    return OutboxMessage(
        id=row.id,
        user_id=row.user_id,
        recipient_id=row.recipient_id,
        payload=dict(row.payload),
        attempts=row.attempts,
        scheduled_at=row.scheduled_at,
        subscription_id=row.subscription_id,
    )


class Store:
    """Таблица `outbox` и метки блокировок в памяти. `rows` и `blocked` — только закоммитенное."""

    def __init__(self, rows: Sequence[Row] = ()) -> None:
        self.rows: dict[int, Row] = {row.id: copy.deepcopy(row) for row in rows}
        # Кто заблокировал бота: Telegram id → когда. Это `users.bot_blocked_at`.
        self.blocked: dict[int, datetime] = {}
        self.events: list[str] = []
        # Строки, запертые чужой транзакцией: `SKIP LOCKED` проходит мимо них.
        self.locked_elsewhere: set[int] = set()
        # Что успела сделать другая копия между планированием прохода и запиранием.
        self.before_lock: Callable[[Store], None] | None = None
        # Отказ базы на шаге с этим именем (имя метода очереди или `commit`):
        # исключение и сколько вызовов пропустить, прежде чем оно сработает.
        self.failures: dict[str, tuple[BaseException, int]] = {}

    def fail(self, step: str, error: BaseException, *, after: int = 0) -> None:
        self.failures[step] = (error, after)

    def scope(self) -> AbstractAsyncContextManager[Work]:
        return self._scope()

    @asynccontextmanager
    async def _scope(self) -> AsyncIterator[Work]:
        unit = Txn(self)
        try:
            yield Work(queue=unit, users=unit, commit=unit.commit)
        finally:
            unit.close()

    def row(self, identifier: int) -> Row:
        return self.rows[identifier]


class Txn:
    """Одна транзакция: правки идут в копию и попадают в `Store` только по `commit`."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._rows = copy.deepcopy(store.rows)
        self._blocked = dict(store.blocked)
        self._dirty = False

    def _step(self, name: str) -> None:
        planned = self._store.failures.get(name)
        if planned is None:
            return
        error, skip = planned
        if skip > 0:
            self._store.failures[name] = (error, skip - 1)
            return
        raise error

    async def commit(self) -> None:
        self._step("commit")
        self._store.rows = copy.deepcopy(self._rows)
        self._store.blocked = dict(self._blocked)
        self._store.events.append("commit")
        self._dirty = False

    def close(self) -> None:
        if self._dirty:
            self._store.events.append("rollback")

    def _due(self, now: datetime | None) -> list[Row]:
        moment = now or START
        rows = [
            r for r in self._rows.values() if r.status == "pending" and r.scheduled_at <= moment
        ]
        return sorted(rows, key=lambda r: (r.scheduled_at, r.id))

    async def take_pending(self, *, limit: int, now: datetime | None = None) -> list[OutboxMessage]:
        self._step("take_pending")
        return [_message(row) for row in self._due(now)[:limit]]

    async def lock_pending(self, ids: Sequence[int], *, now: datetime) -> list[OutboxMessage]:
        self._step("lock_pending")
        if self._store.before_lock is not None:
            self._store.before_lock(self._store)
            self._rows = copy.deepcopy(self._store.rows)
        held = [
            r for r in self._due(now) if r.id in ids and r.id not in self._store.locked_elsewhere
        ]
        self._store.events.append("lock:" + ",".join(str(row.id) for row in held))
        return [_message(row) for row in held]

    async def mark_sent(self, message_id: int, *, now: datetime | None = None) -> None:
        self._step("mark_sent")
        row = self._rows[message_id]
        row.status, row.sent_at, row.last_error = "sent", now, None
        self._store.events.append(f"mark_sent:{message_id}")
        self._dirty = True

    async def mark_failed(
        self, message_id: int, *, retry_at: datetime, error: str | None = None
    ) -> None:
        self._step("mark_failed")
        row = self._rows[message_id]
        row.attempts, row.scheduled_at, row.last_error = row.attempts + 1, retry_at, error
        self._store.events.append(f"mark_failed:{message_id}")
        self._dirty = True

    async def give_up(self, message_id: int, *, error: str | None = None) -> None:
        self._step("give_up")
        row = self._rows[message_id]
        row.status, row.attempts, row.last_error = "failed", row.attempts + 1, error
        self._store.events.append(f"give_up:{message_id}")
        self._dirty = True

    async def cancel_pending_of(self, user_id: int, *, reason: str) -> int:
        self._step("cancel_pending_of")
        return self._cancel([r for r in self._rows.values() if r.user_id == user_id], reason)

    async def cancel_for_blocked_users(self, *, reason: str) -> int:
        self._step("cancel_for_blocked_users")
        rows = [r for r in self._rows.values() if r.recipient_id in self._blocked]
        return self._cancel(rows, reason)

    def _cancel(self, rows: list[Row], reason: str) -> int:
        pending = [row for row in rows if row.status == "pending"]
        for row in pending:
            row.status, row.last_error = "cancelled", reason
        if pending:
            self._store.events.append("cancel:" + ",".join(str(row.id) for row in pending))
            self._dirty = True
        return len(pending)

    async def set_bot_blocked(self, tg_user_id: int, *, blocked: bool, at: datetime) -> int | None:
        self._step("set_bot_blocked")
        if blocked:
            self._blocked.setdefault(tg_user_id, at)
        else:
            self._blocked.pop(tg_user_id, None)
        self._store.events.append(f"{'block' if blocked else 'unblock'}:{tg_user_id}")
        self._dirty = True
        known = [row.user_id for row in self._rows.values() if row.recipient_id == tg_user_id]
        return known[0] if known else None


class Telegram:
    """Подставной Bot API: пишет в журнал, двигает часы и отвечает по сценарию.

    Сценарий — список исходов по порядку вызовов: `None` — отправлено, исключение —
    поднимается. Кончился сценарий — дальше всё отправляется.
    """

    def __init__(
        self,
        store: Store,
        clock: Clock,
        *outcomes: BaseException | None,
        takes: float = 1.0,
    ) -> None:
        self._store = store
        self._clock = clock
        self._outcomes = list(outcomes)
        self._takes = takes
        self.texts: list[str] = []
        self.recipients: list[int] = []

    async def __call__(self, user_id: int, text: str) -> None:
        self._store.events.append(f"send:{user_id}")
        self.texts.append(text)
        self.recipients.append(user_id)
        outcome = self._outcomes.pop(0) if self._outcomes else None
        if outcome is not None:
            raise outcome
        self._clock.advance(seconds=self._takes)


def deliver(
    store: Store, clock: Clock, *outcomes: BaseException | None, policy: Policy | None = None
) -> tuple[Delivery, Telegram]:
    """Нотифаер на очереди в памяти и подставном Bot API; паузы между сообщениями нет."""
    telegram = Telegram(store, clock, *outcomes)
    delivery = Delivery(telegram, pause_s=0.0, clock=clock, scope=store.scope, policy=policy)
    return delivery, telegram
