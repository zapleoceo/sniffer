"""Внутренняя очередь на Postgres.

Брокера сообщений здесь нет сознательно (architecture.md, раздел 3): на этом
объёме Redis или Rabbit добавляют операционную сложность и не решают ни одной
реальной проблемы. Роль брокера играет `SELECT … FOR UPDATE SKIP LOCKED` —
два воркера, читающие очередь одновременно, получают разные задачи, потому что
второй пропускает строку, уже заблокированную первым, вместо того чтобы ждать
её освобождения.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from sniffer.db import models
from sniffer.db.mappers import to_job
from sniffer.db.repositories.base import Repository
from sniffer.domain.records import Job

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

DEFAULT_RETRY = timedelta(minutes=5)


class JobRepository(Repository):
    async def enqueue(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        run_after: datetime | None = None,
    ) -> int:
        row = models.Job(kind=kind, payload=payload or {})
        if run_after is not None:
            row.run_after = run_after
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def take(self, *, kinds: Sequence[str] | None = None) -> Job | None:
        """Взять одну задачу и пометить её выполняющейся.

        Блокировка снимается коммитом вызывающего, поэтому коммитить стоит
        сразу после взятия: строка уже помечена `running`, и держать на ней
        замок всё время обработки незачем.
        """
        conditions = [
            models.Job.status == STATUS_PENDING,
            models.Job.run_after <= datetime.now(UTC),
        ]
        if kinds:
            conditions.append(models.Job.kind.in_(kinds))

        row = await self._session.scalar(
            select(models.Job)
            .where(*conditions)
            .order_by(models.Job.run_after, models.Job.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None

        row.status = STATUS_RUNNING
        row.locked_at = datetime.now(UTC)
        row.attempts += 1
        await self._session.flush()
        return to_job(row)

    async def finish(self, job_id: int) -> None:
        await self._session.execute(
            update(models.Job)
            .where(models.Job.id == job_id)
            .values(status=STATUS_DONE, locked_at=None, last_error=None)
        )

    async def fail(
        self, job_id: int, error: str, *, retry_in: timedelta | None = DEFAULT_RETRY
    ) -> None:
        """Вернуть задачу в очередь или похоронить.

        `retry_in=None` означает «повторять нечего»: задача остаётся `failed`
        с текстом ошибки. Бесконечный повтор сломанной задачи выедает воркер и
        прячет причину — потолок попыток ставит вызывающий, он один знает,
        сколько их осмысленно для его `kind`.
        """
        values: dict[str, Any] = {"last_error": error, "locked_at": None}
        if retry_in is None:
            values["status"] = STATUS_FAILED
        else:
            values["status"] = STATUS_PENDING
            values["run_after"] = datetime.now(UTC) + retry_in
        await self._session.execute(
            update(models.Job).where(models.Job.id == job_id).values(**values)
        )
