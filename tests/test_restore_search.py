"""Возврат убранного поиска владельцем без базы: форма запросов и коды возврата команды.

Тесты на живом Postgres — `test_db_restore_search.py` (в CI). Здесь страховка там, где базы нет:
SQL собирается боевым диалектом и проверяется на условиях, на которых держатся обещания.
"""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from sniffer.db.repositories.slots import PaidSearch, SlotRepository
from sniffer.db.repositories.watch import WatchRepository
from sniffer.worker import restore_search

DIALECT = create_async_engine("postgresql+asyncpg://sniffer:sniffer@localhost/shape").dialect
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


class Result:
    def __init__(self, row: Any = None) -> None:
        self.row = row

    def scalar_one_or_none(self) -> Any:
        return self.row


class Recorder:
    def __init__(self, *, row: Any = None, head: int = 500) -> None:
        self.row, self.head = row, head
        self.sql: list[str] = []

    def _text(self, statement: Any) -> str:
        compiled = statement.compile(dialect=DIALECT, compile_kwargs={"literal_binds": True})
        text = " ".join(str(compiled).split())
        self.sql.append(text)
        return text

    async def scalar(self, statement: Any) -> Any:
        text = self._text(statement)
        return self.head if "max(listings.id)" in text else self.row

    async def execute(self, statement: Any) -> Result:
        self._text(statement)
        return Result(1)


async def test_the_tab_update_is_conditional_on_archived_and_scoped_to_one_user_and_root() -> None:
    session = Recorder()
    assert await WatchRepository(session).restore_archived(7, 3)  # type: ignore[arg-type]
    (sql,) = session.sql
    assert sql.startswith("UPDATE search_tabs SET state=CASE WHEN")
    assert "message_thread_id IS NOT NULL) THEN 'open' ELSE 'lost' END" in sql
    assert "search_tabs.user_id = 7" in sql and "search_tabs.passport_root = 3" in sql
    assert "search_tabs.state = 'archived'" in sql, "не в архиве — строка не меняется"


def monitor(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "id": 11,
        "is_active": False,
        "expires_at": None,
        "no_slot_since": NOW - timedelta(days=3),
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


async def test_resume_keeps_the_pause_mark_so_the_monitor_decides_the_cursor() -> None:
    session = Recorder(row=monitor())
    assert await SlotRepository(session).resume_restored(7, 3, now=NOW) == 11  # type: ignore[arg-type]
    update = session.sql[-1]
    assert update.startswith("UPDATE subscriptions SET is_active=true")
    for untouched in ("no_slot_since", "hard_filter", "since_listing_id", "scan_listing_id"):
        assert untouched not in update.split(" WHERE ")[0], untouched


async def test_resume_without_a_pause_mark_lifts_the_cursor_to_the_head_itself() -> None:
    session = Recorder(row=monitor(no_slot_since=None))
    await SlotRepository(session).resume_restored(7, 3, now=NOW)  # type: ignore[arg-type]
    assert "greatest(subscriptions.scan_listing_id, 500)" in session.sql[-1]


async def test_a_paid_row_is_refused_and_never_written() -> None:
    """После настоящего архива `sync` ставит оплаченной строке `expires_at` на момент архива."""
    session = Recorder(row=monitor(expires_at=NOW - timedelta(hours=1)))
    with pytest.raises(PaidSearch):
        await SlotRepository(session).resume_restored(7, 3, now=NOW)  # type: ignore[arg-type]
    assert not any(item.startswith("UPDATE") for item in session.sql)


async def test_a_search_without_monitoring_is_not_given_one() -> None:
    session = Recorder(row=None)
    assert await SlotRepository(session).resume_restored(7, 3, now=NOW) is None  # type: ignore[arg-type]
    assert not any(item.startswith("UPDATE subscriptions") for item in session.sql)


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    user: bool,
    owns: bool,
    archived: bool,
    paid: bool = False,
    owner: bool = True,
) -> FakeSession:
    session = FakeSession()
    monkeypatch.setattr(
        restore_search, "get_settings", lambda: SimpleNamespace(is_owner=lambda _: owner)
    )

    @asynccontextmanager
    async def scope() -> Any:
        yield session

    class Users:
        def __init__(self, _: Any) -> None: ...

        async def get_by_tg_id(self, tg: int) -> Any:
            return SimpleNamespace(id=5) if user else None

    class Delivery:
        def __init__(self, _: Any) -> None: ...

        async def owns_chain(self, **_: Any) -> bool:
            return owns

    class Watch:
        def __init__(self, _: Any) -> None: ...

        async def restore_archived(self, *_: Any) -> str | None:
            return "lost" if archived else None

    class Slots:
        def __init__(self, _: Any) -> None: ...

        async def resume_restored(self, *_: Any, **__: Any) -> int:
            if paid:
                raise PaidSearch(2)
            return 9

    for name, fake in (
        ("session_scope", scope),
        ("UserRepository", Users),
        ("DeliveryRepository", Delivery),
        ("WatchRepository", Watch),
        ("SlotRepository", Slots),
    ):
        monkeypatch.setattr(restore_search, name, fake)
    return session


@pytest.mark.parametrize(
    ("user", "owns"), [(False, False), (True, False)], ids=["unknown client", "foreign root"]
)
async def test_a_foreign_or_missing_search_is_exit_3_and_commits_nothing(
    monkeypatch: pytest.MonkeyPatch, user: bool, owns: bool
) -> None:
    session = wire(monkeypatch, user=user, owns=owns, archived=True)
    args = argparse.Namespace(tg_user_id=1, root=2)
    assert await restore_search.restore(args, NOW) == restore_search.EXIT_NOT_FOUND
    assert session.commits == 0


async def test_a_non_owner_is_refused_before_any_database_work(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = wire(monkeypatch, user=True, owns=True, archived=True, owner=False)
    assert await restore_search.restore(argparse.Namespace(tg_user_id=1, root=2), NOW) == 3
    assert session.commits == 0
    assert "not the owner" in capsys.readouterr().err


async def test_a_paid_search_is_exit_3_with_a_reason_and_commits_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = wire(monkeypatch, user=True, owns=True, archived=True, paid=True)
    assert await restore_search.restore(argparse.Namespace(tg_user_id=1, root=2), NOW) == 3
    assert session.commits == 0 and "paid search" in capsys.readouterr().err


async def test_a_search_that_is_not_archived_is_a_no_op_with_exit_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = wire(monkeypatch, user=True, owns=True, archived=False)
    assert await restore_search.restore(argparse.Namespace(tg_user_id=1, root=2), NOW) == 0
    assert session.commits == 0 and "nothing to restore" in capsys.readouterr().out


async def test_an_archived_own_search_is_restored_and_committed_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = wire(monkeypatch, user=True, owns=True, archived=True)
    assert await restore_search.restore(argparse.Namespace(tg_user_id=1, root=2), NOW) == 0
    assert session.commits == 1 and "restored root=2 tab=lost subscription=9" in (
        capsys.readouterr().out
    )


def test_missing_arguments_are_usage_not_a_traceback() -> None:
    assert restore_search.main([]) == restore_search.EXIT_USAGE
    assert restore_search.main(["--tg-user-id", "x", "--root", "1"]) == restore_search.EXIT_USAGE
