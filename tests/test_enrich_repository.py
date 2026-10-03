"""Репозиторий догона без живой базы: запрос страницы и обёртка записи.

Что зависит от самого Postgres (`jsonb ||`, SAVEPOINT после отказа
`NUMERIC(14,2)`, перепроверка `WHERE`), проверено на живой базе в
`test_db_enrichment.py`. Здесь — то, что можно проверить без неё и что обязано
краснеть на любой машине: какой запрос уходит на чтение страницы, что запись
идёт в SAVEPOINT, что считается «записано» и что исключение не глотается.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.dialects.postgresql import asyncpg
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db import models
from sniffer.db.repositories.listing_enrichment import (
    ListingEnrichmentRepository,
    page_statement,
    patch_statement,
)
from sniffer.domain.listing_patch import ListingPatch
from tests.enrich_support import card


def compiled(statement: Any) -> tuple[str, dict[str, Any]]:
    result = statement.compile(dialect=asyncpg.dialect())  # type: ignore[no-untyped-call]
    return str(result), dict(result.params)


# ── запрос страницы ────────────────────────────────────────────────────────


def test_the_page_query_reads_active_cards_of_one_source_after_the_cursor_in_id_order() -> None:
    text, params = compiled(page_statement("telegram_archive", after_id=5, limit=200))

    assert "listings.source = $1::VARCHAR" in text
    assert "listings.is_active IS true" in text
    assert "listings.id > $2::BIGINT" in text
    assert "ORDER BY listings.id" in text
    assert "DESC" not in text, "курсор по возрастанию: по убыванию он не продвигается"
    assert "LIMIT $3::INTEGER" in text
    assert params == {"source_1": "telegram_archive", "id_1": 5, "param_1": 200}


def test_the_page_query_keeps_a_card_without_raw_text_by_joining_on_the_left() -> None:
    text, _ = compiled(page_statement("telegram_archive", after_id=0, limit=10))

    assert "LEFT OUTER JOIN raw_messages ON listings.raw_message_id = raw_messages.id" in text
    assert "raw_messages.text" in text


def test_the_page_query_refreshes_what_the_session_remembers() -> None:
    options = page_statement("telegram_archive", after_id=0, limit=1).get_execution_options()

    assert options["populate_existing"] is True


# ── сессия-заглушка ────────────────────────────────────────────────────────


class Nested:
    """`begin_nested()`: записывает, чем кончился SAVEPOINT."""

    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def __aenter__(self) -> Nested:
        self.session.events.append("savepoint")
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self.session.events.append("release" if exc is None else "rollback")
        return False


class FakeSession:
    def __init__(
        self,
        *,
        rowcount: int | None = 1,
        boom: BaseException | None = None,
        rows: list[tuple[models.Listing, str | None]] | None = None,
    ) -> None:
        self.events: list[str] = []
        self.statements: list[Any] = []
        self.rowcount, self.boom, self.rows = rowcount, boom, rows or []

    def begin_nested(self) -> Nested:
        return Nested(self)

    async def execute(self, statement: Any) -> Any:
        self.events.append("execute")
        self.statements.append(statement)
        if self.boom is not None:
            raise self.boom
        return SimpleNamespace(rowcount=self.rowcount, all=lambda: self.rows)


def repository(session: FakeSession) -> ListingEnrichmentRepository:
    return ListingEnrichmentRepository(cast(AsyncSession, session))


PATCH = ListingPatch({"lang": "ru"})


# ── запись ─────────────────────────────────────────────────────────────────


async def test_a_written_row_is_true_and_the_write_runs_inside_its_own_savepoint() -> None:
    session = FakeSession(rowcount=1)

    assert await repository(session).apply(card(), PATCH) is True
    assert session.events == ["savepoint", "execute", "release"]


@pytest.mark.parametrize("rowcount", [0, None, 2])
async def test_anything_but_exactly_one_row_is_not_a_write(rowcount: int | None) -> None:
    """0 — строка изменилась; `None` — драйвер счёта не дал; 2 — условие не по ключу."""
    assert await repository(FakeSession(rowcount=rowcount)).apply(card(), PATCH) is False


async def test_the_statement_that_goes_out_is_the_guarded_update_of_this_card() -> None:
    session = FakeSession()
    row = card(41)

    await repository(session).apply(row, PATCH)

    assert compiled(session.statements[0]) == compiled(patch_statement(row, PATCH))


async def test_a_refusal_rolls_the_savepoint_back_and_is_not_swallowed() -> None:
    class Refused(Exception):
        pass

    session = FakeSession(boom=Refused("numeric field overflow"))

    with pytest.raises(Refused):
        await repository(session).apply(card(), PATCH)

    assert session.events == ["savepoint", "execute", "rollback"]


# ── чтение ─────────────────────────────────────────────────────────────────


def orm_row(listing_id: int) -> models.Listing:
    return models.Listing(
        id=listing_id,
        source="telegram_archive",
        deal_type="rent_out",
        category="apartment",
        city="nha_trang",
        title=f"Объявление {listing_id}",
        summary="сводка",
        tg_link=f"https://t.me/c/1/{listing_id}",
        posted_at=datetime.now(UTC),
        attributes={"rooms": 2},
        confidence=0.4,
        is_active=True,
    )


async def test_a_page_pairs_every_card_with_its_text_and_keeps_the_missing_one_missing() -> None:
    session = FakeSession(rows=[(orm_row(1), "текст"), (orm_row(2), None)])

    page = await repository(session).page("telegram_archive", after_id=0, limit=10)

    assert [(item.listing.id, item.text) for item in page] == [(1, "текст"), (2, None)]
    assert page[0].listing.attributes == {"rooms": 2}
    assert page[0].listing.raw_message_id is None
