"""Переписка с ботом.

Пишется обеими сторонами диалога: без реплик бота лог показывает вопросы без
ответов, и понять, что человек в итоге увидел, невозможно.
"""

from __future__ import annotations

from sqlalchemy import select

from sniffer.db import models
from sniffer.db.mappers import to_dialog_message
from sniffer.db.repositories.base import Repository
from sniffer.domain.records import DIRECTION_IN, DIRECTION_OUT, DialogMessage

# Карточки выдачи — это простыня на несколько килобайт. В лог кладём начало:
# страница всё равно не покажет больше, а таблица от полных копий распухает
# быстрее, чем растёт польза.
MAX_STORED_CHARS = 4000
PAGE_LIMIT = 200


class DialogRepository(Repository):
    async def log_incoming(
        self, user_id: int, text: str, *, request_id: int | None = None
    ) -> DialogMessage:
        return await self._log(user_id, DIRECTION_IN, text, request_id)

    async def log_outgoing(
        self, user_id: int, text: str, *, request_id: int | None = None
    ) -> DialogMessage:
        return await self._log(user_id, DIRECTION_OUT, text, request_id)

    async def by_user(self, user_id: int, *, limit: int = PAGE_LIMIT) -> list[DialogMessage]:
        """Переписка одного клиента, свежее сверху."""
        rows = await self._session.scalars(
            select(models.DialogMessage)
            .where(models.DialogMessage.user_id == user_id)
            .order_by(models.DialogMessage.created_at.desc(), models.DialogMessage.id.desc())
            .limit(min(limit, PAGE_LIMIT))
        )
        return [to_dialog_message(row) for row in rows]

    async def by_request(self, request_id: int) -> list[DialogMessage]:
        """Реплики одного запроса — в порядке разговора, а не наоборот."""
        rows = await self._session.scalars(
            select(models.DialogMessage)
            .where(models.DialogMessage.request_id == request_id)
            .order_by(models.DialogMessage.created_at, models.DialogMessage.id)
        )
        return [to_dialog_message(row) for row in rows]

    async def _log(
        self, user_id: int, direction: str, text: str, request_id: int | None
    ) -> DialogMessage:
        row = models.DialogMessage(
            user_id=user_id,
            direction=direction,
            text=text[:MAX_STORED_CHARS],
            request_id=request_id,
        )
        self._session.add(row)
        await self._session.flush()
        return to_dialog_message(row)
