"""Подключение к Postgres: один engine на процесс, сессия на единицу работы.

Пул живёт в процессе, сессия — нет. Сессия держит открытую транзакцию и
identity map; переиспользованная между вызовами, она копит объекты и рано или
поздно отдаёт клиенту данные, прочитанные полчаса назад. Отсюда правило
CLAUDE.md: `async with` на сессию, коммит явный, между вызовами не
переиспользуем.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sniffer.config import get_settings

# Четыре процесса на одной 4-ядерной машине делят одну базу. По пять
# соединений на процесс — это 40 при потолке Postgres в 100, с запасом на
# psql руками и на pg_dump по крону.
POOL_SIZE = 5
POOL_MAX_OVERFLOW = 5
# Postgres в контейнере переживает рестарт, соединение в пуле — нет.
# pre_ping ловит мёртвое соединение до запроса, recycle не даёт ему протухнуть.
POOL_RECYCLE_S = 1800

_engine: AsyncEngine | None = None
_sessions: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            get_settings().database_url,
            pool_size=POOL_SIZE,
            max_overflow=POOL_MAX_OVERFLOW,
            pool_pre_ping=True,
            pool_recycle=POOL_RECYCLE_S,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessions
    if _sessions is None:
        # expire_on_commit=False: репозиторий уже отдал наружу dataclass, и
        # повторный поход в базу за теми же полями после коммита не нужен.
        _sessions = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessions


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Сессия на одну единицу работы. Коммит — за вызывающим.

    Автокоммита здесь нет намеренно: он делает границу транзакции невидимой в
    коде, и «прочитал и ничего не менял» становится неотличимо от «записал».
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Закрыть пул при остановке процесса."""
    global _engine, _sessions
    engine, _engine, _sessions = _engine, None, None
    if engine is not None:
        await engine.dispose()
