"""Расходы на LLM: токены и стоимость каждого вызова.

Пишем свои данные, а не спрашиваем брокер при каждом просмотре: страница,
зависящая от доступности чужого сервиса, показывает пустоту ровно в тот момент,
когда на неё смотрят из-за инцидента (docs/dashboard.md).
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

from sqlalchemy import Table, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sniffer.db import models
from sniffer.db.mappers import broker_call_values, to_broker_call
from sniffer.db.repositories.base import Repository
from sniffer.domain.records import BrokerCall

PAGE_LIMIT = 200


class BrokerCallRepository(Repository):
    async def record(self, call: BrokerCall) -> BrokerCall | None:
        """Записать расход. Повтор того же `broker_request_id` игнорируется.

        Через `ON CONFLICT DO NOTHING`, а не «проверил — вставил»: поллинг
        отдаёт завершённую задачу повторно при ретрае сети, и двойной учёт
        стоимости выглядел бы как перерасход, которого не было.
        """
        table = cast(Table, models.BrokerCall.__table__)
        inserted = await self._session.scalar(
            pg_insert(table)
            .values(**broker_call_values(call))
            .on_conflict_do_nothing(index_elements=["broker_request_id"])
            .returning(table.c.id)
        )
        if inserted is None:
            return None
        row = await self._session.get(models.BrokerCall, inserted)
        if row is None:  # pragma: no cover — строка вставлена в этой же транзакции
            raise LookupError(f"вставленный расход {inserted} не читается")
        return to_broker_call(row)

    async def by_request(self, request_id: int) -> list[BrokerCall]:
        rows = await self._session.scalars(
            select(models.BrokerCall)
            .where(models.BrokerCall.request_id == request_id)
            .order_by(models.BrokerCall.created_at, models.BrokerCall.id)
        )
        return [to_broker_call(row) for row in rows]

    async def cost_by_request(self, request_ids: list[int]) -> dict[int, tuple[int, Decimal]]:
        """Токены и стоимость по каждому из перечисленных запросов.

        Одним запросом на всю страницу вместо запроса на строку: пятьдесят
        строк лога — это пятьдесят обращений к базе на каждый просмотр.
        """
        if not request_ids:
            return {}
        rows = await self._session.execute(
            select(
                models.BrokerCall.request_id,
                func.sum(models.BrokerCall.tokens_in + models.BrokerCall.tokens_out),
                func.sum(models.BrokerCall.cost_usd),
            )
            .where(models.BrokerCall.request_id.in_(request_ids))
            .group_by(models.BrokerCall.request_id)
        )
        return {
            int(request_id): (int(tokens or 0), Decimal(cost or 0))
            for request_id, tokens, cost in rows.all()
        }

    async def totals(self) -> dict[str, object]:
        """Сводка расходов за всё время. Для блока общей статистики."""
        row = (
            await self._session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(models.BrokerCall.tokens_in), 0),
                    func.coalesce(func.sum(models.BrokerCall.tokens_out), 0),
                    func.coalesce(func.sum(models.BrokerCall.cost_usd), 0),
                )
            )
        ).one()
        return {
            "calls": int(row[0]),
            "tokens_in": int(row[1]),
            "tokens_out": int(row[2]),
            "cost_usd": Decimal(row[3]),
        }
