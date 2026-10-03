"""Форма запросов вкладок без живого Postgres: условия, на которых держатся темы."""

from __future__ import annotations

from collections.abc import Iterator

from sniffer.db.repositories.tabs import TabRepository
from tests.test_watch_sql_shape import Recorder, Result


class Rows(Result):
    def __iter__(self) -> Iterator[tuple[int, int]]:
        return iter([(4, 555)])


class Session(Recorder):
    async def execute(self, statement: object) -> Result:
        self._text(statement)
        return Rows(1)

    async def scalar(self, statement: object) -> int:
        self._text(statement)
        return 1


async def test_a_thread_is_served_only_while_the_link_is_open() -> None:
    session = Session()
    await TabRepository(session).root_of(7, 555)  # type: ignore[arg-type]
    (sql,) = session.sql
    assert "search_tabs.user_id = 7" in sql
    assert "search_tabs.message_thread_id = 555" in sql
    assert "search_tabs.state = 'open'" in sql


async def test_claiming_never_overwrites_an_existing_link() -> None:
    session = Session()
    await TabRepository(session).claim(7, 3, 555, "x")  # type: ignore[arg-type]
    (sql,) = session.sql
    assert sql.startswith("INSERT INTO search_tabs") and "ON CONFLICT DO NOTHING" in sql


async def test_attach_overwrites_a_link_but_never_an_archived_one() -> None:
    session = Session()
    await TabRepository(session).attach(7, 3, 555, "x")  # type: ignore[arg-type]
    (sql,) = session.sql
    assert "ON CONFLICT (user_id, passport_root) DO UPDATE" in sql
    assert "WHERE search_tabs.state != 'archived'" in sql


async def test_the_notifier_asks_only_for_open_links_with_a_thread() -> None:
    session = Session()
    await TabRepository(session).threads_for([4, 5])  # type: ignore[arg-type]
    (sql,) = session.sql
    assert "subscriptions.id IN (4, 5)" in sql
    assert "search_tabs.state = 'open'" in sql
    assert "message_thread_id IS NOT NULL" in sql


async def test_losing_a_link_touches_only_an_open_one() -> None:
    session = Session()
    await TabRepository(session).mark_lost(4)  # type: ignore[arg-type]
    (sql,) = session.sql
    assert sql.startswith("UPDATE search_tabs SET state='lost'")
    assert "search_tabs.state = 'open'" in sql
