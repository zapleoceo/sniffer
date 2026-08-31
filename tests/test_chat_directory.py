"""Обёртка реестра чатов: сессия, город, обрезка.

Базы здесь нет — репозиторий и фабрика сессии подставляются. Проверяется
именно то, чего нет в `ChatRepository`: фильтр по городу и границы выборки.
Формы `Chat` и `ChatRepository` из ветки слоя `db` воспроизведены дословно —
если они разъедутся с протоколами, это должно быть видно тестом, а не после
мержа.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime

from sniffer.sources.chat_directory import CITY_OVERFETCH, RepositoryChatDirectory


@dataclass(frozen=True, slots=True)
class Chat:
    """Копия `sniffer.domain.records.Chat` из ветки `feat/db-layer`."""

    tg_id: int
    title: str
    city: str
    username: str | None = None
    categories: list[str] = field(default_factory=list)
    is_active: bool = True
    search_rank: int = 100
    msg_count_24h: int = 0
    last_msg_id: int = 0
    last_synced_at: datetime | None = None
    added_at: datetime | None = None
    id: int | None = None


@dataclass
class FakeSession:
    closed: bool = False


@dataclass
class FakeRepository:
    """`ChatRepository` в той части, которой пользуется обёртка."""

    chats: Sequence[Chat]
    session: FakeSession
    asked: list[int] = field(default_factory=list)

    async def list_active(self, *, limit: int = 10) -> list[Chat]:
        assert not self.session.closed, "репозиторий работает в закрытой сессии"
        self.asked.append(limit)
        return list(self.chats)[:limit]


@dataclass
class Sessions:
    """`session_scope` без базы: считает открытия и закрытия."""

    opened: int = 0
    last: FakeSession | None = None

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[FakeSession]:
        self.opened += 1
        self.last = FakeSession()
        try:
            yield self.last
        finally:
            self.last.closed = True


def directory(chats: Sequence[Chat]) -> tuple[RepositoryChatDirectory, Sessions, list[int]]:
    sessions = Sessions()
    asked: list[int] = []

    def repository(session: FakeSession) -> FakeRepository:
        repo = FakeRepository(chats, session, asked)
        return repo

    # Фабрики типизированы под AsyncSession, а тест подсовывает свою сессию:
    # обёртке сессия нужна как ключ, она в неё не заглядывает.
    return (
        RepositoryChatDirectory(sessions, repository),  # type: ignore[arg-type]
        sessions,
        asked,
    )


def chat(tg_id: int, city: str, rank: int = 100) -> Chat:
    return Chat(tg_id=tg_id, title=f"чат {tg_id}", city=city, search_rank=rank)


async def test_only_the_asked_city_comes_back() -> None:
    """У репозитория фильтра по городу нет — город отрезает обёртка."""
    registry = [chat(-100_1, "nha_trang"), chat(-100_2, "da_nang"), chat(-100_3, "nha_trang")]
    chats, _, _ = directory(registry)
    found = await chats.list_active(city="nha_trang", limit=10)
    assert [c.tg_id for c in found] == [-100_1, -100_3]


async def test_city_is_filtered_after_the_query_so_we_ask_for_more() -> None:
    """Без запаса десять чатов Нячанга превратились бы в два.

    Репозиторий отдаёт активные чаты всех городов подряд; попроси мы ровно
    десять — восемь мест заняли бы чужие города.
    """
    registry = [chat(-100_0 - n, "da_nang") for n in range(40)]
    registry += [chat(-200_0 - n, "nha_trang") for n in range(5)]
    chats, _, asked = directory(registry)
    found = await chats.list_active(city="nha_trang", limit=10)

    assert asked == [10 * CITY_OVERFETCH]
    assert [c.tg_id for c in found] == [-200_0 - n for n in range(5)]


async def test_result_is_cut_to_the_budget() -> None:
    """Потолок обхода — не пожелание: одиннадцатый чат это FloodWait."""
    chats, _, _ = directory([chat(-100_0 - n, "nha_trang") for n in range(30)])
    found = await chats.list_active(city="nha_trang", limit=10)
    assert len(found) == 10


async def test_chat_without_city_is_not_excluded() -> None:
    """Пустой город означает «не указан» и поиск не сужает."""
    chats, _, _ = directory([chat(-100_1, ""), chat(-100_2, "da_nang")])
    found = await chats.list_active(city="nha_trang", limit=10)
    assert [c.tg_id for c in found] == [-100_1]


async def test_session_is_opened_and_closed_per_call() -> None:
    """Сессия живёт одну единицу работы (CLAUDE.md), между вызовами — нет."""
    chats, sessions, _ = directory([chat(-100_1, "nha_trang")])
    await chats.list_active(city="nha_trang", limit=10)
    await chats.list_active(city="nha_trang", limit=10)

    assert sessions.opened == 2
    assert sessions.last is not None
    assert sessions.last.closed is True
