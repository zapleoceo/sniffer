"""Постоянное состояние безопасной разведки Telegram-чатов.

Здесь только SQL и доменные записи. Решение, можно ли вступать, остаётся в
`sources/telegram_discover_joiner.py`; этот репозиторий даёт ему атомарные
кирпичи, чтобы лимиты не зависели от памяти процесса.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from sniffer.db import models
from sniffer.db.repositories.base import Repository
from sniffer.domain.records import (
    CandidateState,
    DiscoveryCandidate,
    JoinEvent,
    JoinLimits,
    RejectedCandidate,
)

# Один короткий transaction-level замок на весь аккаунт. Он не держится во
# время Telegram-вызова: защищает лишь проверку лимитов и запись слота.
JOIN_LOCK_KEY = 5_021_260


class CandidateRepository(Repository):
    async def push(self, candidate: DiscoveryCandidate) -> None:
        await self._session.execute(
            insert(models.ChatCandidate)
            .values(
                key=candidate.key,
                username=candidate.username or None,
                invite_hash=candidate.invite_hash or None,
                found_in=candidate.found_in,
                priority=candidate.priority,
            )
            .on_conflict_do_nothing(index_elements=[models.ChatCandidate.key])
        )

    async def reserve(self) -> DiscoveryCandidate | None:
        row = await self._session.scalar(
            select(models.ChatCandidate)
            .where(models.ChatCandidate.status == "queued")
            .order_by(models.ChatCandidate.priority, models.ChatCandidate.found_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        row.status = "joining"
        await self._session.flush()
        return _candidate(row)

    async def release(self, key: str) -> int:
        row = await self._session.scalar(
            select(models.ChatCandidate).where(models.ChatCandidate.key == key).with_for_update()
        )
        if row is None:
            return 0
        row.status = "queued"
        row.attempts += 1
        await self._session.flush()
        return row.attempts

    async def drop(self, key: str) -> None:
        statement = delete(models.ChatCandidate).where(models.ChatCandidate.key == key)
        await self._session.execute(statement)

    async def contains(self, key: str) -> bool:
        return bool(
            await self._session.scalar(
                select(models.ChatCandidate.id).where(models.ChatCandidate.key == key).limit(1)
            )
        )

    async def snapshot(self, *, limit: int = 100) -> list[CandidateState]:
        """Очередь так, как её разбирает `reserve()`: приоритет, потом возраст.

        Порядок здесь не украшение. Владелец смотрит на эту таблицу, чтобы
        понять, кто следующий и почему очередь не двигается, — а не двигается
        она ровно тогда, когда первый по этому порядку копит `attempts`.
        """
        rows = await self._session.scalars(
            select(models.ChatCandidate)
            .order_by(models.ChatCandidate.priority, models.ChatCandidate.found_at)
            .limit(limit)
        )
        return [_state(row) for row in rows]

    async def counts_by_status(self) -> dict[str, int]:
        rows = await self._session.execute(
            select(models.ChatCandidate.status, func.count()).group_by(models.ChatCandidate.status)
        )
        return {str(status): int(total) for status, total in rows}


class RejectRepository(Repository):
    async def contains(self, key: str) -> bool:
        return bool(
            await self._session.scalar(
                select(models.ChatReject.key).where(models.ChatReject.key == key).limit(1)
            )
        )

    async def recent(self, *, limit: int = 50) -> list[RejectedCandidate]:
        """Кого и почему не взяли. Причина — та же строка, что пишет joiner."""
        rows = await self._session.scalars(
            select(models.ChatReject).order_by(models.ChatReject.rejected_at.desc()).limit(limit)
        )
        return [
            RejectedCandidate(key=row.key, reason=row.reason, rejected_at=row.rejected_at)
            for row in rows
        ]

    async def reject(self, key: str, reason: str) -> None:
        await self._session.execute(
            insert(models.ChatReject)
            .values(key=key, reason=reason)
            .on_conflict_do_nothing(index_elements=[models.ChatReject.key])
        )


class JoinLedgerRepository(Repository):
    async def state(self, now: datetime, *, window: timedelta) -> JoinLimits:
        since = now - window
        joins = int(
            await self._session.scalar(
                select(func.count(models.ChatJoinEvent.id)).where(
                    models.ChatJoinEvent.happened_at >= since
                )
            )
            or 0
        )
        next_allowed = await self._session.scalar(
            select(func.max(models.ChatJoinEvent.next_allowed_at)).where(
                models.ChatJoinEvent.next_allowed_at.is_not(None)
            )
        )
        blocked = await self._session.scalar(
            select(func.max(models.ChatJoinEvent.blocked_until)).where(
                models.ChatJoinEvent.blocked_until > now
            )
        )
        return JoinLimits(joins, next_allowed, blocked)

    async def claim_slot(
        self,
        now: datetime,
        *,
        window: timedelta,
        maximum: int,
        next_allowed_at: datetime,
    ) -> int | None:
        await self._session.execute(select(func.pg_advisory_xact_lock(JOIN_LOCK_KEY)))
        state = await self.state(now, window=window)
        if (
            state.joins_in_window >= maximum
            or state.blocked_until is not None
            or (state.next_allowed_at is not None and state.next_allowed_at > now)
        ):
            return None
        row = models.ChatJoinEvent(happened_at=now, next_allowed_at=next_allowed_at)
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def confirm_join(self, event_id: int, *, tg_id: int, username: str) -> None:
        await self._session.execute(
            update(models.ChatJoinEvent)
            .where(models.ChatJoinEvent.id == event_id)
            .values(kind="joined", tg_id=tg_id, username=username or None)
        )

    async def release_slot(self, event_id: int) -> None:
        statement = delete(models.ChatJoinEvent).where(models.ChatJoinEvent.id == event_id)
        await self._session.execute(statement)

    async def record_flood(self, event_id: int, *, blocked_until: datetime) -> None:
        await self._session.execute(
            update(models.ChatJoinEvent)
            .where(models.ChatJoinEvent.id == event_id)
            .values(kind="flood", blocked_until=blocked_until)
        )

    async def record_mute_failure(self, *, tg_id: int, error: str) -> None:
        await self._session.execute(
            update(models.ChatJoinEvent)
            .where(models.ChatJoinEvent.tg_id == tg_id, models.ChatJoinEvent.kind == "joined")
            .values(mute_error=error)
        )

    async def mark_muted(self, *, tg_id: int) -> None:
        await self._session.execute(
            update(models.ChatJoinEvent)
            .where(models.ChatJoinEvent.tg_id == tg_id, models.ChatJoinEvent.kind == "joined")
            .values(muted=True, mute_error=None)
        )

    async def recent_events(self, *, limit: int = 30) -> list[JoinEvent]:
        """Журнал вступлений как есть — включая `claimed` без исхода.

        Строка `claimed` без `tg_id` и есть потраченный впустую слот: она
        осталась после обрыва или после ответа, из которого чата не достали.
        Прятать её нельзя — именно по ней видно, куда ушли суточные попытки.
        """
        rows = await self._session.scalars(
            select(models.ChatJoinEvent)
            .order_by(models.ChatJoinEvent.happened_at.desc())
            .limit(limit)
        )
        return [
            JoinEvent(
                id=row.id,
                kind=row.kind,
                tg_id=row.tg_id,
                username=row.username,
                happened_at=row.happened_at,
                next_allowed_at=row.next_allowed_at,
                blocked_until=row.blocked_until,
                muted=row.muted,
                mute_error=row.mute_error,
            )
            for row in rows
        ]

    async def pending_mutes(self) -> list[int]:
        rows = await self._session.scalars(
            select(models.ChatJoinEvent.tg_id).where(
                models.ChatJoinEvent.kind == "joined",
                models.ChatJoinEvent.muted.is_(False),
                models.ChatJoinEvent.tg_id.is_not(None),
            )
        )
        return [tg_id for tg_id in rows if tg_id is not None]


def _candidate(row: models.ChatCandidate) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        key=row.key,
        username=row.username or "",
        invite_hash=row.invite_hash or "",
        found_in=row.found_in,
        priority=row.priority,
    )


def _state(row: models.ChatCandidate) -> CandidateState:
    return CandidateState(
        key=row.key,
        username=row.username or "",
        invite_hash=row.invite_hash or "",
        found_in=row.found_in,
        priority=row.priority,
        status=row.status,
        attempts=row.attempts,
        found_at=row.found_at,
    )
