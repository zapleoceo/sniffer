"""Каждый путь снятия карточки пишет флаг, время и причину вместе (022), без Postgres.

Подставная сессия записывает запросы текстом диалекта Postgres. Живое поведение — в
`test_db_repositories.py` и CI; здесь видно, что НИ ОДИН путь не гасит карточку «молча».
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from sniffer.db.repositories.listings import ListingRepository
from sniffer.domain.listing_state import REASON_SUPERSEDED, unseen_reason
from sniffer.domain.records import Listing

POSTGRES = postgresql.dialect()  # type: ignore[no-untyped-call]


class Result:
    rowcount = 1


class StubSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Result:
        self.statements.append(statement.compile(dialect=POSTGRES))
        return Result()

    async def flush(self) -> None: ...

    async def get(self, *args: object) -> None:
        return None


def repo() -> tuple[ListingRepository, StubSession]:
    session = StubSession()
    return ListingRepository(session), session  # type: ignore[arg-type]


def squeezed(compiled: Any) -> str:
    return str(compiled).replace(" ", "").replace("\n", "")


def assert_deactivated(compiled: Any, reason: str) -> None:
    text = squeezed(compiled)
    assert "is_active=false" in text or "is_active=%(is_active)s" in text
    assert "deactivated_at=now()" in text
    assert reason in compiled.params.values(), compiled.params


async def test_age_expiry_writes_expired() -> None:
    listings, session = repo()

    await listings.expire(older_than=datetime(2026, 1, 1, tzinfo=UTC), limit=10)

    assert_deactivated(session.statements[-1], "expired")


async def test_the_model_verdict_writes_screen_only_when_it_rejects() -> None:
    listings, session = repo()

    await listings.apply_screen(1, keep=False, note="обмен валют")
    assert_deactivated(session.statements[-1], "screen")

    await listings.apply_screen(2, keep=True, note="ок")
    assert "deactivated" not in str(session.statements[-1]), "оставленная карточка без причины"


@pytest.mark.parametrize("reason", ["liveness_deleted", "liveness_closed"])
async def test_deactivate_many_writes_the_reason_it_is_given(reason: str) -> None:
    listings, session = repo()

    await listings.deactivate_many([1, 2], reason=reason)

    assert_deactivated(session.statements[-1], reason)


async def test_deactivate_many_without_ids_writes_nothing() -> None:
    listings, session = repo()

    assert await listings.deactivate_many([], reason="liveness_deleted") == 0
    assert session.statements == []


async def test_a_card_absent_from_the_board_writes_the_source_reason() -> None:
    listings, session = repo()

    await listings.retire_unseen(
        "chotot", city="nha_trang", category="motorbike", seen={"a"}, reason=unseen_reason("chotot")
    )

    assert_deactivated(session.statements[-1], "chotot_unseen")


async def test_replacing_a_card_writes_superseded() -> None:
    listings, session = repo()

    await listings.deactivate(5)

    assert_deactivated(session.statements[-1], REASON_SUPERSEDED)


async def test_refreshing_an_active_card_clears_the_old_reason() -> None:
    listings, session = repo()
    replacement = Listing(
        raw_message_id=1,
        summary="s",
        source="telegram_archive",
        deal_type="rent_out",
        category="motorbike",
        city="nha_trang",
        title="t",
        tg_link="https://t.me/x/1",
        posted_at=datetime(2026, 10, 1, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="listing_not_found"):
        await listings.refresh(5, replacement)

    params = session.statements[0].params
    assert params["deactivated_at"] is None and params["deactivated_reason"] is None
