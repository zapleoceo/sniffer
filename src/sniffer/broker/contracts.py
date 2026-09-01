"""Контракты брокера, не зависящие от хранения.

Клиент AIbroker обязан быть пригоден для маленьких утилит и тестов без
Postgres. Поэтому тип приёмника учёта живёт в модуле-листе: импорт клиента не
должен загружать ``broker.usage``, а тот — SQLAlchemy и репозитории.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sniffer.broker.client import BrokerResult


class UsageSink(Protocol):
    """Куда клиент сообщает о завершившемся платном вызове."""

    async def __call__(self, capability: str, result: BrokerResult) -> None: ...
