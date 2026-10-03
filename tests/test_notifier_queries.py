"""Форма запросов очереди: что они запирают и что отбирают — без базы.

Живой Postgres в CI проверяет поведение (`test_notifier_db.py`), но локально он
не поднимается, а ошибка в одном условии запроса — это не падение, а доставка не
той строки. Поэтому ключевые условия проверяются и по тексту SQL, скомпилированному
под Postgres: убрали `SKIP LOCKED` или условие по сроку — тест краснеет там, где
базы нет.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.db.repositories.users import UserRepository

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


class Nothing:
    """Пустой результат запроса: ни строк, ни скаляра."""

    def __iter__(self) -> Iterator[Any]:
        return iter(())

    def all(self) -> list[Any]:
        return []

    def scalar_one_or_none(self) -> None:
        return None


class RecordingSession:
    """Сессия, которая запоминает запросы и ничего не возвращает."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Nothing:
        self.statements.append(statement)
        return Nothing()

    async def scalar(self, statement: Any) -> None:
        self.statements.append(statement)


def repository() -> tuple[DeliveryRepository, RecordingSession]:
    session = RecordingSession()
    return DeliveryRepository(cast(AsyncSession, session)), session


def sql(statement: Any) -> str:
    compiled = statement.compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    return " ".join(str(compiled).split())


def params(statement: Any) -> dict[str, Any]:
    compiled = statement.compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    return dict(compiled.params)


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


async def test_cancelling_a_clients_queue_touches_only_their_waiting_rows() -> None:
    repo, session = repository()

    await repo.cancel_pending_of(7, reason="forbidden")

    text = sql(session.statements[0])
    assert text.startswith("UPDATE outbox SET status="), text
    assert "outbox.status =" in text and "outbox.user_id =" in text, "чужие и ушедшие строки целы"


async def test_cancelling_for_blocked_clients_selects_them_by_the_block_mark() -> None:
    repo, session = repository()

    await repo.cancel_for_blocked_users(reason="bot_blocked")

    text = sql(session.statements[0])
    assert (
        "outbox.user_id IN (SELECT users.id FROM users WHERE users.bot_blocked_at IS NOT NULL)"
        in text
    )
    assert "outbox.status =" in text


async def test_the_matcher_selection_skips_clients_who_blocked_the_bot() -> None:
    repo, session = repository()

    await repo.active_subscriptions(now=NOW)

    text = sql(session.statements[0])
    assert "JOIN users ON users.id = subscriptions.user_id" in text, text
    assert "users.bot_blocked_at IS NULL" in text, "слежение за заблокировавшим — пустые запросы"


async def test_giving_up_counts_the_attempt_and_keeps_the_reason() -> None:
    repo, session = repository()

    await repo.give_up(5, error="too_long: ...")

    text = sql(session.statements[0])
    assert "attempts=(outbox.attempts +" in text and "last_error=" in text, text
    assert "status=" in text


async def test_a_failed_attempt_keeps_the_reason_and_moves_the_retry_time() -> None:
    repo, session = repository()

    await repo.mark_failed(5, retry_at=NOW, error="transient: ...")

    text = sql(session.statements[0])
    assert "attempts=(outbox.attempts +" in text and "scheduled_at=" in text, text
    assert "last_error=" in text and "status=" not in text, "статус остаётся pending"


async def test_blocking_keeps_the_earliest_moment_and_unblocking_clears_it() -> None:
    session = RecordingSession()
    users = UserRepository(cast(AsyncSession, session))

    await users.set_bot_blocked(42, blocked=True, at=NOW)
    await users.set_bot_blocked(42, blocked=False, at=NOW)

    blocking, unblocking = (sql(statement) for statement in session.statements)
    assert "coalesce(users.bot_blocked_at," in blocking.lower(), "повторный отказ сдвинул бы момент"
    assert params(session.statements[1])["bot_blocked_at"] is None, unblocking
    assert "coalesce" not in unblocking.lower(), "снятие безусловно: клиент снова доступен"
    assert "users.tg_user_id =" in blocking and "RETURNING users.id" in blocking


async def test_a_confirmed_send_stamps_the_moment_and_forgets_an_earlier_failure() -> None:
    repo, session = repository()

    await repo.mark_sent(5, now=NOW)

    text = sql(session.statements[1])
    assert text.startswith("UPDATE outbox SET status="), text
    assert "sent_at=" in text and "last_error=" in text, (
        "старая причина осталась бы у ушедшей строки"
    )
    assert params(session.statements[1])["sent_at"] == NOW
