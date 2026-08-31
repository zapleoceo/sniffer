"""Общий каркас репозитория.

Репозиторий не владеет сессией и не коммитит: он получает уже открытую и
работает в её транзакции. Так вызывающий сам решает, где граница единицы
работы, — например, вставка карточки и снятие задачи с очереди коммитятся
вместе или не коммитятся вовсе.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class Repository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
