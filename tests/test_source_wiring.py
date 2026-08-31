"""Проводка: источник, полученный БОЕВЫМ способом, читает чаты из базы.

Отдельный файл, потому что проверяется здесь не поведение адаптера, а сборка.
Адаптер был написан, задокументирован и покрыт тестами, а боевой поиск всё
равно находил ноль: `run_plan` создаёт источник через `get_source(name)` без
аргументов, и реестр чатов ему доставался заглушкой. Тесты на обёртку эту дыру
пропустили — они проверяли, что `RepositoryChatDirectory` умеет отдавать чаты,
а не что кто-нибудь его создаёт.

Поэтому тесты ниже идут по настоящему пути и подменяют ровно две вещи, которых
в тестах быть не может: сеть Telegram и адрес базы. Всё между ними —
`run_plan` → реестр источников → адаптер → `new_directory()` →
`sniffer.db.session_scope` + `ChatRepository` — настоящее.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import sniffer.db as db
from sniffer.config import Settings
from sniffer.db.repositories import ChatRepository
from sniffer.domain.records import Chat
from sniffer.search.live import run_plan
from sniffer.search.plan import SearchPlan, SearchTask
from sniffer.sources import telegram_groups
from sniffer.sources.base import get_source
from sniffer.sources.chat_directory import RepositoryChatDirectory, new_directory
from sniffer.sources.telegram_reference import SOURCE_NAME

# Чат, который лежит в базе и не лежит нигде в коде: если он доехал до находки,
# значит источник действительно прочитал реестр, а не выдумал его.
CHAT_TG_ID = -1001657234891
CHAT_USERNAME = "nhatrang_baraholka"
MSG_ID = 55120
TEXT = "Продам Honda Lead 2019, блюкарт на руках, 13 млн донгов."

# Настройки, при которых адаптер считает юзербота заведённым. Значения
# ненастоящие: до Telegram запрос не доходит, клиент подменён.
CONFIGURED = Settings(tg_api_id=1, tg_api_hash="hash", tg_session="session")


@dataclass(frozen=True, slots=True)
class Message:
    """Сообщение Telethon в объёме протокола `MessageLike`."""

    id: int = MSG_ID
    message: str | None = TEXT
    date: datetime | None = datetime(2026, 8, 29, 4, 11, 7, tzinfo=UTC)
    media: object | None = None
    grouped_id: int | None = None
    reply_to: None = None


@dataclass
class FakeTelegram:
    """Telethon без сети и с разрешённой поверхностью."""

    messages: list[Message] = field(default_factory=lambda: [Message()])
    queried: list[object] = field(default_factory=list)

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def get_messages(
        self,
        entity: int | str,
        *,
        search: str,
        limit: int,
    ) -> Sequence[Message]:
        self.queried.append(entity)
        return self.messages

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"юзербот дёрнул запрещённый метод Telegram: {name}")


@dataclass
class FakeChatRepository:
    """`ChatRepository` без базы — на месте настоящего в `sniffer.db`."""

    session: object
    asked: list[int] = field(default_factory=list)

    async def list_active(self, *, limit: int = 10) -> list[Chat]:
        self.asked.append(limit)
        return [
            Chat(
                tg_id=CHAT_TG_ID,
                title="Нячанг · Барахолка",
                city="nha_trang",
                username=CHAT_USERNAME,
                search_rank=10,
            )
        ]


@asynccontextmanager
async def fake_session_scope() -> AsyncIterator[object]:
    yield object()


def telegram_offline(monkeypatch: pytest.MonkeyPatch, client: FakeTelegram) -> None:
    """Убрать сеть с боевого пути создания клиента, не тронув сам путь.

    `new_reader` берёт `TelegramClient` и `StringSession` из Telethon в момент
    вызова, поэтому подменяются именно эти два имени: всё остальное в
    `telegram_client.py` работает как в бою — включая `flood_sleep_threshold=0`.
    """
    monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: client)
    monkeypatch.setattr("telethon.sessions.StringSession", lambda value: value)
    monkeypatch.setattr(telegram_groups, "get_settings", lambda: CONFIGURED)


def db_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подменить адрес базы, оставив саму сборку `new_directory()` настоящей."""
    monkeypatch.setattr(db, "session_scope", fake_session_scope)
    monkeypatch.setattr(db, "ChatRepository", FakeChatRepository)


def plan() -> SearchPlan:
    return SearchPlan(
        tasks=[SearchTask(source=SOURCE_NAME, query="байк", params={"city": "nha_trang"})]
    )


def test_assembly_reaches_the_db_layer() -> None:
    """Сборка из spec-v2 4.4 существует и собирается без базы под рукой.

    Дешёвая страховка от переименования в `sniffer.db`: сам импорт внутри
    `new_directory` иначе развалился бы только в бою, где ошибку реестра
    источник проглатывает и помечает себя `degraded`.
    """
    assert isinstance(new_directory(), RepositoryChatDirectory)


async def test_run_plan_finds_a_chat_that_exists_only_in_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Боевой `run_plan` доходит до чата из реестра и приносит находку.

    Тест, который падал до проводки: источник получал заглушку, писал в лог
    `telegram.no_chat_directory` и возвращал ноль находок.
    """
    client = FakeTelegram()
    telegram_offline(monkeypatch, client)
    db_offline(monkeypatch)

    items = await run_plan(plan())

    assert [item.external_id for item in items] == [f"{CHAT_TG_ID}:{MSG_ID}"]
    assert items[0].url == f"https://t.me/{CHAT_USERNAME}/{MSG_ID}"
    # Адресуем чат по username из записи реестра: по голому id сущность
    # разрешается только из кэша сессии (spec-v2, 4.4).
    assert client.queried == [CHAT_USERNAME]


async def test_registry_is_asked_with_the_city_of_the_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Город из задачи доезжает до обёртки, а та просит выборку с запасом."""
    asked: list[int] = []

    class Recording(FakeChatRepository):
        async def list_active(self, *, limit: int = 10) -> list[Chat]:
            asked.append(limit)
            return await super().list_active(limit=limit)

    telegram_offline(monkeypatch, FakeTelegram())
    monkeypatch.setattr(db, "session_scope", fake_session_scope)
    monkeypatch.setattr(db, "ChatRepository", Recording)

    await run_plan(plan())
    assert asked == [50], "10 чатов бюджета × пятикратный запас на фильтр по городу"


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL не задан: живого Postgres нет",
)
async def test_source_reads_a_chat_written_to_postgres(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Тот же путь, но реестр — настоящая таблица `chats` в Postgres.

    Здесь подменён только адрес базы: сессию открывает боевой `new_directory`,
    SQL выполняет настоящий `ChatRepository`.
    """
    await ChatRepository(db_session).add(
        Chat(
            tg_id=CHAT_TG_ID,
            title="Нячанг · Барахолка",
            city="nha_trang",
            username=CHAT_USERNAME,
            search_rank=10,
        )
    )
    await db_session.commit()

    sessions = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def test_scope() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    monkeypatch.setattr(db, "session_scope", test_scope)

    client = FakeTelegram()
    source = get_source(SOURCE_NAME, client=client)
    items = await source.search("байк", {"city": "nha_trang"})

    assert [item.external_id for item in items] == [f"{CHAT_TG_ID}:{MSG_ID}"]
    assert client.queried == [CHAT_USERNAME]
