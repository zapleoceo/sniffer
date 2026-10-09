"""Исключение группы из сбора по решению владельца и возврат обратно (020).

Единственное место, где пишутся `chats.excluded_*` и `chat_exclusion_events`. Ни одного
вызова Telegram: исключение — запись в нашей базе, из группы мы не выходим.

Границу транзакции держит вызывающий (`Repository` не коммитит): статус чата и строка
журнала либо легли вместе, либо не легли вовсе. Журнал только добавляется — UPDATE и
DELETE по `chat_exclusion_events` в коде нет.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select, update

from sniffer.db import models
from sniffer.db.repositories.base import Repository

EXCLUDE, RESTORE = "exclude", "restore"


class Outcome(StrEnum):
    DONE = "done"  # состояние изменено, событие записано
    NOOP = "noop"  # чат уже в запрошенном состоянии: второго события нет
    NOT_FOUND = "not_found"  # такого tg_id в реестре нет


class ChatExclusionRepository(Repository):
    async def exclude(
        self,
        tg_id: int,
        *,
        reason: str,
        evidence: dict[str, Any],
        actor: str,
        now: datetime | None = None,
    ) -> Outcome:
        """Вывести чат из сбора: `is_active=false` + `excluded_*` + событие журнала.

        Курсоры (`last_msg_id`, `backfill_msg_id`, `backfill_done`) не трогаются, а их
        значение на момент решения кладётся в снимок: после restore они продолжат с того же
        места, и по снимку видно, докуда чат был дочитан, когда решали.
        """
        moment = now or datetime.now(UTC)
        chat = await self._locked(tg_id)
        if chat is None:
            return Outcome.NOT_FOUND
        if chat.excluded_at is not None:
            return Outcome.NOOP
        snapshot = {
            "cursors": {
                "last_msg_id": chat.last_msg_id,
                "backfill_msg_id": chat.backfill_msg_id,
                "backfill_done": chat.backfill_done,
            },
            **evidence,
        }
        await self._session.execute(
            update(models.Chat)
            .where(models.Chat.id == chat.id)
            .values(
                is_active=False,
                excluded_at=moment,
                excluded_reason=reason,
                excluded_evidence=snapshot,
            )
        )
        self._session.add(
            models.ChatExclusionEvent(
                tg_id=tg_id,
                action=EXCLUDE,
                reason=reason,
                evidence=snapshot,
                actor=actor,
                at=moment,
            )
        )
        await self._session.flush()
        return Outcome.DONE

    async def restore(
        self, tg_id: int, *, reason: str, actor: str, now: datetime | None = None
    ) -> Outcome:
        """Вернуть чат в сбор. Курсоры не трогаем: чтение продолжится с того же места.

        Снимок доказательств из строки чата обнуляется, но не теряется: он остаётся в
        событии `exclude`, а в событие `restore` попадает копия «чем было исключено».
        """
        moment = now or datetime.now(UTC)
        chat = await self._locked(tg_id)
        if chat is None:
            return Outcome.NOT_FOUND
        if chat.excluded_at is None:
            return Outcome.NOOP
        was = {
            "was_excluded_at": chat.excluded_at.isoformat(),
            "was_reason": chat.excluded_reason,
            "was_evidence": chat.excluded_evidence,
        }
        await self._session.execute(
            update(models.Chat)
            .where(models.Chat.id == chat.id)
            .values(is_active=True, excluded_at=None, excluded_reason=None, excluded_evidence=None)
        )
        self._session.add(
            models.ChatExclusionEvent(
                tg_id=tg_id, action=RESTORE, reason=reason, evidence=was, actor=actor, at=moment
            )
        )
        await self._session.flush()
        return Outcome.DONE

    async def history(self, tg_id: int) -> list[models.ChatExclusionEvent]:
        rows = await self._session.scalars(
            select(models.ChatExclusionEvent)
            .where(models.ChatExclusionEvent.tg_id == tg_id)
            .order_by(models.ChatExclusionEvent.id)
        )
        return list(rows)

    async def _locked(self, tg_id: int) -> models.Chat | None:
        """Строка чата под `FOR UPDATE`: два параллельных exclude не напишут два события."""
        row = await self._session.scalar(
            select(models.Chat).where(models.Chat.tg_id == tg_id).with_for_update()
        )
        return row
