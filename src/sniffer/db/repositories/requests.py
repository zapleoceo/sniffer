"""Запросы клиентов: лог для дашборда и якорь для расходов.

Запись открывается до работы и закрывается после: у зависшего или упавшего
запроса тоже есть строка, иначе в логе видно только удачные — то есть ровно не
то, на что смотрят при разборе.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from sniffer.db import models
from sniffer.db.mappers import to_client_request
from sniffer.db.repositories.base import Repository
from sniffer.domain.records import (
    REQUEST_DONE,
    REQUEST_FAILED,
    ClientRequest,
)

# Дашборд — страница для одного человека, но лог запросов растёт линейно.
# Потолок нужен, чтобы страница не начала тянуть всю таблицу через полгода.
PAGE_LIMIT = 50


class ClientRequestRepository(Repository):
    async def open(
        self, user_id: int, raw_query: str, *, passport_id: int | None = None
    ) -> ClientRequest:
        row = models.ClientRequest(user_id=user_id, raw_query=raw_query, passport_id=passport_id)
        self._session.add(row)
        await self._session.flush()
        return to_client_request(row)

    async def finish(
        self,
        request_id: int,
        *,
        result_count: int = 0,
        stages: dict[str, int] | None = None,
        plan_fallback: bool = False,
        sources: list[str] | None = None,
        passport_id: int | None = None,
        error: str | None = None,
    ) -> ClientRequest | None:
        """Закрыть запрос. `error` не пуст — статус `failed`, а не `done`.

        Длительность считаем в базе от `started_at`, а не по своему таймеру:
        часы процесса и часы Postgres расходятся, и разъехавшись, они дают
        отрицательное время выполнения в логе.
        """
        row = await self._session.get(models.ClientRequest, request_id)
        if row is None:
            return None

        finished = datetime.now(UTC)
        row.finished_at = finished
        row.duration_ms = max(0, int((finished - row.started_at).total_seconds() * 1000))
        row.status = REQUEST_FAILED if error else REQUEST_DONE
        row.error = error
        row.result_count = result_count
        row.plan_fallback = plan_fallback
        if stages is not None:
            row.stages = dict(stages)
        if sources is not None:
            row.sources = list(sources)
        if passport_id is not None:
            row.passport_id = passport_id
        await self._session.flush()
        return to_client_request(row)

    async def get(self, request_id: int) -> ClientRequest | None:
        row = await self._session.get(models.ClientRequest, request_id)
        return to_client_request(row) if row is not None else None

    async def recent(self, *, limit: int = PAGE_LIMIT) -> list[ClientRequest]:
        rows = await self._session.scalars(
            select(models.ClientRequest)
            .order_by(models.ClientRequest.started_at.desc(), models.ClientRequest.id.desc())
            .limit(min(limit, PAGE_LIMIT))
        )
        return [to_client_request(row) for row in rows]

    async def counts_by_user(self) -> dict[int, int]:
        """Сколько запросов у каждого клиента. Для таблицы пользователей."""
        rows = await self._session.execute(
            select(models.ClientRequest.user_id, func.count()).group_by(
                models.ClientRequest.user_id
            )
        )
        return {int(user_id): int(count) for user_id, count in rows.all()}

    async def totals(self) -> dict[str, int]:
        """Сводка по запросам: сколько всего, сколько упало, сколько фолбэков.

        Считаем одним запросом: три отдельных `count(*)` по одной таблице —
        три полных обхода ради трёх чисел.
        """
        row = (
            await self._session.execute(
                select(
                    func.count(),
                    func.count().filter(models.ClientRequest.status == REQUEST_FAILED),
                    func.count().filter(models.ClientRequest.plan_fallback.is_(True)),
                    func.coalesce(func.avg(models.ClientRequest.duration_ms), 0),
                )
            )
        ).one()
        return {
            "requests": int(row[0]),
            "failed": int(row[1]),
            "fallbacks": int(row[2]),
            "avg_duration_ms": int(row[3]),
        }
