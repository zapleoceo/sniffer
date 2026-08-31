"""Общие фикстуры.

Живая база подключается только когда задан `TEST_DATABASE_URL`. Схема
Postgres-специфична — `JSONB`, `TEXT[]`, `tsvector`, `vector(1024)` — и на
sqlite не поднимается в принципе, так что подменять её нечем: подделка
проверяла бы подделку, а не наш SQL. Локально без Docker такие тесты
пропускаются, в CI поднимается `pgvector/pgvector:pg16`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sniffer.db.models import Base

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """Чистая база на каждый тест.

    Чистим не TRUNCATE-строкой, а метаданными в обратном порядке зависимостей:
    правило проекта «SQL только в db/» распространяется и на тесты, иначе
    список таблиц разъедется со схемой молча.
    """
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL не задан: живого Postgres нет")

    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                await conn.execute(table.delete())
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as session:
        yield session
