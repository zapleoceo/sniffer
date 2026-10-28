"""News progress is independent of marketplace stage and protects originals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db.models import Base
from sniffer.db.repositories.news_processing import NewsProcessingRepository
from sniffer.db.repositories.raw_messages import RawMessageRepository
from sniffer.domain.records import RawMessage


def test_news_state_has_separate_key_and_restricts_original_deletion() -> None:
    table = Base.metadata.tables["news_processing"]
    assert table.c.raw_message_id.primary_key
    assert next(iter(table.c.raw_message_id.foreign_keys)).ondelete == "RESTRICT"
    assert "stage" not in table.c


async def test_retention_query_excludes_news_references_without_a_database() -> None:
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    repository = RawMessageRepository(cast(AsyncSession, session))

    assert await repository.delete_expired(older_than=datetime.now(UTC), limit=10) == 0

    statement = session.execute.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]
    assert "LEFT OUTER JOIN news_processing" in sql
    assert "news_processing.raw_message_id IS NULL" in sql


async def test_rejected_marketplace_original_can_have_idempotent_news_state(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    originals = RawMessageRepository(db_session)
    ids = await originals.add_many(
        [
            RawMessage(chat_tg_id=100, msg_id=1, text="one", text_hash="one", posted_at=now),
            RawMessage(chat_tg_id=100, msg_id=2, text="two", text_hash="two", posted_at=now),
        ]
    )
    await originals.set_stage(ids[:1], "rejected")
    progress = NewsProcessingRepository(db_session)
    assert await progress.register(ids[0])
    assert not await progress.register(ids[0])
    assert await progress.status(ids[0]) == "pending"
    await db_session.commit()

    retained = await originals.get_by_key(100, 1)
    assert retained is not None and retained.stage == "rejected"
    assert await originals.delete_expired(older_than=now + timedelta(days=1), limit=10) == 1
    assert await originals.get_by_key(100, 1) is not None
    assert await originals.get_by_key(100, 2) is None
