"""Форма запросов репозитория слотов на подставной сессии: порядок и условия, без Postgres.

Локально живой базы нет, поэтому здесь проверяется то, что видно по тексту SQL (диалект
Postgres) и порядку вызовов: замок на клиента берётся ПЕРВЫМ, срок «живой» подписки считается по
переданному «сейчас», пишутся только изменившиеся строки. Поведение под настоящей базой
проверяют `test_slots_db.py` и CI.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.dialects import postgresql

from sniffer.db import models
from sniffer.db.repositories.slots import SlotRepository
from sniffer.domain.slots import Outcome

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)  # расчёт чистый: базу этот модуль не трогает
DAY = timedelta(days=1)
POSTGRES = postgresql.dialect()  # type: ignore[no-untyped-call]


class Rows:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def scalars(self) -> list[Any]:
        return self._items


class StubSession:
    """Записывает каждый запрос текстом Postgres; отдаёт заготовленные строки."""

    def __init__(self, subscriptions: list[models.Subscription], ends: list[datetime]) -> None:
        self.subscriptions = subscriptions
        self.ends = ends
        self.log: list[str] = []
        self.added: list[Any] = []
        self.values: list[dict[str, Any]] = []

    @staticmethod
    def sql(statement: Any) -> str:
        return str(statement.compile(dialect=POSTGRES))

    async def execute(self, statement: Any) -> Rows:
        text = self.sql(statement)
        self.log.append(text)
        compiled = statement.compile(dialect=POSTGRES)
        if text.startswith("UPDATE"):
            self.values.append(dict(compiled.params))
        if "FROM subscriptions" in text and text.startswith("SELECT"):
            return Rows(self.subscriptions)
        return Rows([])

    async def scalars(self, statement: Any) -> list[Any]:
        self.log.append(self.sql(statement))
        return self.ends

    async def scalar(self, statement: Any) -> Any:
        self.log.append(self.sql(statement))
        return 500

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.log.append("FLUSH")


def monitor(ident: int, root: int, *, priority: int = 0, expires: datetime | None) -> Any:
    return models.Subscription(
        id=ident,
        user_id=1,
        passport_root=root,
        priority=priority,
        expires_at=expires,
        is_active=True,
    )


def repo(session: StubSession) -> SlotRepository:
    return SlotRepository(session)  # type: ignore[arg-type]


async def test_the_client_is_locked_before_anything_is_read_or_written() -> None:
    session = StubSession([monitor(1, 10, expires=NOW)], [NOW + 5 * DAY])

    await repo(session).sync(1, NOW)

    assert "FOR UPDATE" in session.log[0] and "FROM users" in session.log[0]


async def test_a_recount_writes_only_the_rows_whose_term_changed() -> None:
    kept = monitor(1, 10, expires=NOW + 5 * DAY)
    moved = monitor(2, 20, priority=1, expires=NOW + 9 * DAY)
    session = StubSession([kept, moved], [NOW + 9 * DAY, NOW + 5 * DAY])

    state = await repo(session).sync(1, NOW)

    updates = [text for text in session.log if text.startswith("UPDATE subscriptions")]
    assert len(updates) == 2, "старшему — долгий срок, младшему — короткий: оба поменялись"
    assert state.slots == 2 and state.holding == 2


async def test_an_unchanged_layout_writes_nothing() -> None:
    session = StubSession([monitor(1, 10, expires=NOW + 5 * DAY)], [NOW + 5 * DAY])

    await repo(session).sync(1, NOW)

    assert not any(text.startswith("UPDATE") for text in session.log)


async def test_live_terms_are_read_with_the_callers_now_not_the_database_clock() -> None:
    session = StubSession([], [])

    await repo(session).sync(1, NOW)

    payments = next(text for text in session.log if "FROM payments" in text)
    assert "now()" not in payments.lower()
    assert "payments.status =" in payments and "GROUP BY payments.invoice_payload" in payments


async def test_enabling_a_new_branch_creates_one_row_from_the_newest_listing() -> None:
    session = StubSession([], [NOW + 30 * DAY])

    outcome, _ = await repo(session).enable(1, 77, NOW)

    assert outcome is Outcome.ENABLE
    (row,) = session.added
    assert (row.passport_root, row.priority, row.since_listing_id) == (77, 0, 500)
    assert row.scan_listing_id == 500 and row.is_active is True


async def test_nothing_is_created_when_there_is_no_slot() -> None:
    session = StubSession([], [])

    outcome, _ = await repo(session).enable(1, 77, NOW)

    assert outcome is Outcome.NEEDS_SUBSCRIPTION and session.added == []


async def test_a_move_reorders_priorities_and_recounts_after_both_writes() -> None:
    source = monitor(1, 10, priority=0, expires=NOW + 5 * DAY)
    session = StubSession([source], [NOW + 5 * DAY])

    moved = await repo(session).move(1, to_root=20, from_root=10, now=NOW)

    assert moved is True
    (row,) = session.added
    assert row.passport_root == 20 and row.priority == -1
    priority_updates = [t for t in session.log if t.startswith("UPDATE") and "priority" in t]
    assert priority_updates, "прежний мониторинг понижен"
    flush_at = session.log.index("FLUSH")
    first_update = next(i for i, t in enumerate(session.log) if t.startswith("UPDATE"))
    assert first_update < flush_at, "понижение записано до создания целевого мониторинга"


async def test_an_impossible_move_writes_nothing() -> None:
    session = StubSession([monitor(1, 10, expires=NOW - DAY)], [])

    moved = await repo(session).move(1, to_root=20, from_root=10, now=NOW)

    assert moved is False and session.added == []
    assert not any(text.startswith("UPDATE") for text in session.log)
