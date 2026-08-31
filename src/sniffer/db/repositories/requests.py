"""Запросы клиентов: лог для дашборда и якорь для расходов.

Запись открывается до работы и закрывается после: у зависшего или упавшего
запроса тоже есть строка, иначе в логе видно только удачные — то есть ровно не
то, на что смотрят при разборе.
"""

from __future__ import annotations

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.sql.elements import Cast

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

        Обе точки времени берём в базе: `started_at` там и стоит, а конец
        считает `clock_timestamp()` в самом UPDATE. Часы процесса не участвуют
        вообще — иначе их расхождение с часами Postgres давало бы отрицательную
        длительность, а `max(0, …)` не лечил бы её, а прятал.
        """
        row = await self._session.get(models.ClientRequest, request_id)
        if row is None:
            return None

        # `clock_timestamp()`, а не `now()`: второе возвращает время НАЧАЛА
        # транзакции, и когда открытие с закрытием попадают в одну (тесты,
        # короткий запрос), длительность выходила бы ровно нулём.
        elapsed_ms = func.round(
            func.extract("epoch", func.clock_timestamp() - models.ClientRequest.started_at) * 1000
        )
        # `cast` до INT обязателен: `round()` над double в Postgres остаётся
        # double, а колонка целая. Тип выписан явно — иначе mypy выводит
        # ожидаемый из колонки (`int | None`) и не принимает `Integer`.
        duration: Cast[int] = cast(elapsed_ms, Integer)
        row.finished_at = func.clock_timestamp()
        row.duration_ms = duration
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
        # Значения, посчитанные в SQL, после flush помечены устаревшими, и
        # обычное чтение атрибута полезло бы в базу лениво — а в async это
        # `MissingGreenlet`, а не запрос. Забираем их явно и здесь же.
        await self._session.refresh(row, ["finished_at", "duration_ms"])
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
