"""Учёт расходов на LLM: вызов брокера → строка в `broker_calls`.

Отдельно от клиента брокера намеренно. Клиент не должен знать про базу — иначе
его не собрать в тесте без Postgres, — поэтому он получает приёмник учёта как
функцию, а «писать в базу» живёт здесь. Обратной зависимости нет: этот модуль
знает про `BrokerResult` только на уровне типов.

К какому запросу клиента отнести расход, знает не брокер и не поиск, а бот:
`intake` и `planner` вызывают модель через несколько слоёв, и протаскивать
`request_id` параметром пришлось бы через сигнатуры, которым он не нужен.
Поэтому идентификатор лежит в contextvar, который бот выставляет на время
обработки одного сообщения. Contextvar, а не глобальная переменная: у каждой
задачи asyncio свой контекст, и два клиента одновременно не перепутают расходы.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from sniffer.db.engine import session_scope
from sniffer.db.repositories.broker_calls import BrokerCallRepository
from sniffer.domain.records import BrokerCall

if TYPE_CHECKING:  # pragma: no cover — только для аннотаций
    from sniffer.broker.client import BrokerResult

log = structlog.get_logger(__name__)

_current_request: ContextVar[int | None] = ContextVar("sniffer_client_request", default=None)


@contextmanager
def request_scope(request_id: int | None) -> Iterator[None]:
    """На время блока все вызовы брокера относятся к этому запросу клиента."""
    token = _current_request.set(request_id)
    try:
        yield
    finally:
        _current_request.reset(token)


def current_request_id() -> int | None:
    return _current_request.get()


def to_broker_call(capability: str, result: BrokerResult) -> BrokerCall:
    """Ответ брокера → доменная запись расхода.

    Стоимость переводим в `Decimal` через строку: `Decimal(float)` тащит за
    собой двоичную погрешность, и суммы по сотне вызовов расходятся с суммой у
    брокера в последних знаках.
    """
    return BrokerCall(
        capability=capability,
        request_id=current_request_id(),
        broker_request_id=result.request_id,
        provider=result.provider,
        model=result.model,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost_usd=Decimal(str(result.cost_usd)) if result.cost_usd is not None else Decimal(0),
        latency_ms=result.latency_ms,
    )


async def default_usage_sink(capability: str, result: BrokerResult) -> None:
    """Записать расход в базу. Своя транзакция на вызов.

    Своя, а не транзакция вызывающего: вызов брокера случается посреди поиска,
    и учёт уже состоявшегося платного вызова не должен откатываться вместе с
    неудачей того, что происходило после него.
    """
    call = to_broker_call(capability, result)
    async with session_scope() as session:
        recorded = await BrokerCallRepository(session).record(call)
        await session.commit()
    if recorded is None:
        # Повтор того же broker_request_id: поллинг вернул завершённую задачу
        # второй раз. Не ошибка — но заметить это стоит.
        log.debug("broker.usage_duplicate", broker_request_id=result.request_id)


async def discard_usage(capability: str, result: BrokerResult) -> None:
    """Приёмник, который ничего не пишет. Для тестов и разовых скриптов."""
    return None
