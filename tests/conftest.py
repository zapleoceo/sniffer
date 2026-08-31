"""Общие фикстуры.

Живая база подключается только когда задан `TEST_DATABASE_URL`. Схема
Postgres-специфична — `JSONB`, `TEXT[]`, `tsvector`, `vector(1024)` — и на
sqlite не поднимается в принципе, так что подменять её нечем: подделка
проверяла бы подделку, а не наш SQL. Локально без Docker такие тесты
пропускаются, в CI поднимается `pgvector/pgvector:pg16`.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sniffer.config import reload_settings
from sniffer.dashboard import auth
from sniffer.db.models import Base

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")

# Значения для тестов дашборда. Токен выдуманный: настоящему в репозитории
# места нет ни в коде, ни в тестах.
BOT_TOKEN = "123456:AAsniffer-test-bot-token"
OWNER = 169510539
STRANGER = 111222333
SESSION_SECRET = "session-secret-длинный-и-случайный-32+"


@dataclass(frozen=True, slots=True)
class DashboardEnv:
    """Окружение владельца и фабрика подписей виджета.

    Фикстурой, а не константами в файле теста: алгоритм подписи — это знание, и
    второй его копии в репозитории быть не должно, иначе они разъедутся.
    """

    bot_token: str
    owner: int
    stranger: int
    session_secret: str

    def sign(
        self,
        user_id: int | None = None,
        *,
        auth_date: int | None = None,
        token: str | None = None,
    ) -> dict[str, str]:
        """Настоящая подпись Telegram Login Widget: HMAC ключом `sha256(токен)`."""
        fields = {
            "id": str(self.owner if user_id is None else user_id),
            "first_name": "Владелец",
            "username": "zapleosoft",
            "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        }
        check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
        key = hashlib.sha256((token or self.bot_token).encode()).digest()
        fields["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        return fields


@pytest.fixture
def dashboard_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[DashboardEnv]:
    """Настроенный владелец плюс чистая память процесса между тестами."""
    monkeypatch.setenv("BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("OWNER_CHAT_ID", str(OWNER))
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("TG_PHONE", "+84900000000")
    reload_settings()
    auth.forget_used_widget_hashes()
    yield DashboardEnv(
        bot_token=BOT_TOKEN, owner=OWNER, stranger=STRANGER, session_secret=SESSION_SECRET
    )
    # Кэш настроек живёт в процессе: не сбросив его, следующий тест увидел бы
    # окружение предыдущего.
    reload_settings()


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
