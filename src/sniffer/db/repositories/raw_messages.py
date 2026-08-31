"""Сырьё из чатов: приём пачками и движение по ступеням воронки."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from sqlalchemy import Table, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sniffer.db import models
from sniffer.db.mappers import raw_message_values, to_raw_message
from sniffer.db.repositories.base import Repository
from sniffer.domain.records import STAGE_PENDING, RawMessage


class RawMessageRepository(Repository):
    async def add_many(self, messages: Sequence[RawMessage]) -> list[int]:
        """Ступень 0 воронки: дедуп на вставке.

        `ON CONFLICT (chat_tg_id, msg_id) DO NOTHING` — потому что коллектор
        перечитывает историю после каждого простоя и приносит уже виденное.
        Считать заранее, что новое, значило бы делать SELECT на каждое
        сообщение; уникальный индекс делает это одним запросом и без гонки.

        `RETURNING` отдаёт только реально вставленные строки — по нему и видно,
        сколько из пачки оказалось новым.
        """
        if not messages:
            return []

        # Ядерный INSERT по таблице, а не по ORM-классу: пачка на тысячу
        # сообщений не должна проезжать через unit of work, ей нужен один
        # запрос.
        table = cast(Table, models.RawMessage.__table__)
        stmt = (
            pg_insert(table)
            .values([raw_message_values(message) for message in messages])
            .on_conflict_do_nothing(index_elements=["chat_tg_id", "msg_id"])
            .returning(table.c.id)
        )
        return list(await self._session.scalars(stmt))

    async def get_by_key(self, chat_tg_id: int, msg_id: int) -> RawMessage | None:
        row = await self._session.scalar(
            select(models.RawMessage).where(
                models.RawMessage.chat_tg_id == chat_tg_id,
                models.RawMessage.msg_id == msg_id,
            )
        )
        return to_raw_message(row) if row is not None else None

    async def list_by_stage(
        self, stage: str = STAGE_PENDING, *, limit: int = 100
    ) -> list[RawMessage]:
        """Что взять в работу следующей ступени. Свежее — первым."""
        rows = await self._session.scalars(
            select(models.RawMessage)
            .where(models.RawMessage.stage == stage)
            .order_by(models.RawMessage.posted_at.desc())
            .limit(limit)
        )
        return [to_raw_message(row) for row in rows]

    async def set_stage(self, ids: Sequence[int], stage: str) -> None:
        if not ids:
            return
        await self._session.execute(
            update(models.RawMessage).where(models.RawMessage.id.in_(ids)).values(stage=stage)
        )
