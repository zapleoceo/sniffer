"""Сырьё из чатов: приём пачками и движение по ступеням воронки."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast

from sqlalchemy import Table, func, select, update
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

    async def recent(self, *, limit: int = 30) -> list[RawMessage]:
        """Последнее собранное сырьё — доказательство, что ингест идёт.

        Порядок по времени публикации, а не по `id`: пачки догона приходят
        вразнобой, и «последнее по вставке» показало бы старое сообщение из
        только что подключённого чата.
        """
        rows = await self._session.scalars(
            select(models.RawMessage).order_by(models.RawMessage.posted_at.desc()).limit(limit)
        )
        return [to_raw_message(row) for row in rows]

    async def counts_by_chat(self) -> dict[int, int]:
        """Сколько сырья принёс каждый чат. Один запрос, а не по чату на строку."""
        rows = await self._session.execute(
            select(models.RawMessage.chat_tg_id, func.count()).group_by(
                models.RawMessage.chat_tg_id
            )
        )
        return {int(chat_tg_id): int(total) for chat_tg_id, total in rows}

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

    async def delete_expired(self, *, older_than: datetime, limit: int) -> int:
        """Убрать протухшее сырьё пачкой. Возврат — сколько строк удалено.

        Два условия, и оба обязательны.

        **По `ingested_at`, а не по `posted_at`.** Срок здесь про диск: «мы
        держим скачанное столько-то», а не «объявление устарело». По дате
        публикации догон истории удалялся бы ровно с той скоростью, с какой
        приходит: коллектор дочитывает архив чата на годы назад, и такая
        уборка стёрла бы его в тот же проход.

        **Только то, из чего не вышло карточки.** У `listings.raw_message_id`
        стоит `ON DELETE CASCADE` — удаление сырья унесло бы с собой живую
        карточку, показанную клиенту, вместе с текстом, по которому verifier
        её сверяет. Поэтому строки с карточкой не трогаем вовсе; их срок
        жизни решает сама карточка, а не возраст сырья.

        Пачкой, а не одним `DELETE`: на первой уборке накопленного за месяцы
        один запрос держал бы длинную транзакцию и таблицу под замком.
        """
        table = cast(Table, models.RawMessage.__table__)
        expired = (
            select(table.c.id)
            .outerjoin(models.Listing, models.Listing.raw_message_id == table.c.id)
            .where(table.c.ingested_at < older_than, models.Listing.id.is_(None))
            .limit(limit)
        )
        deleted = await self._session.execute(
            table.delete().where(table.c.id.in_(expired.scalar_subquery())).returning(table.c.id)
        )
        return len(deleted.scalars().all())

    async def set_stage(
        self, ids: Sequence[int], stage: str, *, gate_signals: dict[str, object] | None = None
    ) -> None:
        if not ids:
            return
        values: dict[str, object] = {"stage": stage}
        if gate_signals is not None:
            values["gate_signals"] = gate_signals
        await self._session.execute(
            update(models.RawMessage).where(models.RawMessage.id.in_(ids)).values(**values)
        )
