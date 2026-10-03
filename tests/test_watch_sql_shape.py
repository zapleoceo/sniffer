"""Форма запросов слежений: страховка там, где живого Postgres нет (`test_watch_db.py` — в CI).

Тесты с базой молча пропускаются без `TEST_DATABASE_URL`, и сломанный запрос выглядел бы
зелёным прогоном. Здесь SQL, который репозиторий отправляет базе, собирается диалектом
боевого драйвера и проверяется грубо: условия, на которых держатся обещания (чужой поиск не
трогаем, просроченный слот не слот, архивный поиск не возвращается).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import create_async_engine

from sniffer.db.repositories.passports import _overviews
from sniffer.db.repositories.watch import Move, WatchRepository

DIALECT = create_async_engine("postgresql+asyncpg://sniffer:sniffer@localhost/shape").dialect
MOMENT = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


class Result:
    def __init__(self, row: Any = None) -> None:
        self.row = row

    def first(self) -> Any:
        return self.row

    def scalar_one_or_none(self) -> Any:
        return self.row


class Recorder:
    """Подставная сессия: пишет SQL по порядку и отвечает настолько, чтобы код дошёл."""

    def __init__(self, *, source: int | None = 5) -> None:
        self.source = source
        self.sql: list[str] = []

    def _text(self, statement: Any) -> str:
        compiled = statement.compile(dialect=DIALECT, compile_kwargs={"literal_binds": True})
        text = " ".join(str(compiled).split())
        self.sql.append(text)
        return text

    async def scalar(self, statement: Any) -> Any:
        text = self._text(statement)
        return self.source if "subscriptions.id" in text else 1  # 1 — «поиск клиента»

    async def execute(self, statement: Any) -> Result:
        self._text(statement)
        return Result()


async def test_a_move_checks_ownership_then_locks_and_resets_the_monitor_state() -> None:
    session = Recorder()
    outcome = await WatchRepository(session).move_slot(  # type: ignore[arg-type]
        user_id=7, from_root=1, to_root=2, now=MOMENT
    )

    assert outcome is Move.MOVED
    owns, source, target, update = session.sql
    assert "passports.user_id = 7" in owns
    assert "FOR UPDATE" in source and "subscriptions.user_id = 7" in source
    assert "subscriptions.expires_at >" in source  # просроченный слот не переносится
    assert "FOR UPDATE" in target and "subscriptions.passport_root = 2" in target
    for fragment in (
        "passport_root=2",
        "failed_streak=0",
        "last_error=NULL",
        "quarantined_until=NULL",
    ):
        assert fragment in update.replace(" ", ""), fragment
    assert re.search(r"max\(listings\.id\)", update)  # курсор — «сейчас», а не старый


async def test_a_move_without_a_live_slot_writes_nothing() -> None:
    session = Recorder(source=None)
    outcome = await WatchRepository(session).move_slot(  # type: ignore[arg-type]
        user_id=7, from_root=1, to_root=2, now=MOMENT
    )
    assert outcome is Move.NO_SLOT
    assert not any(s.startswith(("UPDATE", "DELETE")) for s in session.sql)


async def test_archive_pauses_upserts_the_mark_and_resets_the_pointer_in_this_order() -> None:
    session = Recorder()
    assert await WatchRepository(session).archive(7, 3)  # type: ignore[arg-type]
    owns, pause, mark, pointer = session.sql
    assert "passports.user_id = 7" in owns
    assert pause.startswith("UPDATE subscriptions SET is_active=false")
    assert "subscriptions.user_id = 7" in pause and "subscriptions.passport_root = 3" in pause
    assert (
        mark.startswith("INSERT INTO search_tabs")
        and "ON CONFLICT (user_id, passport_root)" in mark
    )
    assert "'archived'" in mark
    assert pointer.startswith("UPDATE users") and "active_passport_root=NULL" in pointer.replace(
        " ", ""
    )
    assert "active_passport_root = 3" in pointer


def test_archived_searches_are_filtered_out_of_the_menu_query() -> None:
    sql = str(_overviews(7).compile(dialect=DIALECT, compile_kwargs={"literal_binds": True}))
    flat = " ".join(sql.split())
    assert "NOT (EXISTS" in flat and "search_tabs.state = 'archived'" in flat
