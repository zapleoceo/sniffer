"""Idempotent registration of originals for a future, disabled news consumer."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sniffer.db import models
from sniffer.db.repositories.base import Repository


class NewsProcessingRepository(Repository):
    async def register(self, raw_message_id: int) -> bool:
        """Create independent pending state; leave marketplace stage untouched."""
        statement = (
            pg_insert(models.news_processing)
            .values(raw_message_id=raw_message_id)
            .on_conflict_do_nothing(index_elements=["raw_message_id"])
            .returning(models.news_processing.c.raw_message_id)
        )
        return await self._session.scalar(statement) is not None

    async def status(self, raw_message_id: int) -> str | None:
        result = await self._session.scalar(
            select(models.news_processing.c.status).where(
                models.news_processing.c.raw_message_id == raw_message_id
            )
        )
        return str(result) if result is not None else None
