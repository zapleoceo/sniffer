"""Форма запросов очереди: что они запирают и что отбирают — без базы.

Живой Postgres в CI проверяет поведение (`test_notifier_db.py`), но локально он
не поднимается, а ошибка в одном условии запроса — это не падение, а доставка не
той строки. Поэтому ключевые условия проверяются и по тексту SQL, скомпилированному
под Postgres: убрали `SKIP LOCKED` или условие по сроку — тест краснеет там, где
базы нет.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db.repositories.delivery import DeliveryRepository

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


class RecordingSession:
    """Сессия, которая запоминает запросы и ничего не возвращает."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> list[Any]:
        self.statements.append(statement)
        return []


def repository() -> tuple[DeliveryRepository, RecordingSession]:
    session = RecordingSession()
    return DeliveryRepository(cast(AsyncSession, session)), session


def sql(statement: Any) -> str:
    compiled = statement.compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    return " ".join(str(compiled).split())


async def test_lock_pending_locks_only_outbox_rows_and_skips_foreign_locks() -> None:
    repo, session = repository()

    await repo.lock_pending([3, 1], now=NOW)

    text = sql(session.statements[0])
    assert "FOR UPDATE OF outbox SKIP LOCKED" in text, "две копии нотифаера пошлют дважды"
    assert "outbox.id IN" in text and "outbox.status =" in text, text
    assert "outbox.scheduled_at <=" in text, "отложенную другой копией строку слать нельзя"


async def test_lock_pending_without_ids_does_not_touch_the_database() -> None:
    repo, session = repository()

    assert await repo.lock_pending([], now=NOW) == []
    assert session.statements == []


async def test_take_pending_only_reads_so_the_pass_holds_no_locks() -> None:
    repo, session = repository()

    await repo.take_pending(limit=20, now=NOW)

    text = sql(session.statements[0])
    assert "FOR UPDATE" not in text, "блокировка на проход держала бы строки десятки секунд"
    assert "ORDER BY outbox.scheduled_at, outbox.id" in text and "LIMIT" in text
