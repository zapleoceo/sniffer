"""Реестр чатов для источника: обёртка над репозиторием, сборка и заглушка.

Тонкая прослойка между `ChatRepository` (слой `db`) и адаптером. Нужна из-за
трёх расхождений, каждое из которых иначе пришлось бы чинить правкой чужой
ветки:

1. Репозиторий не владеет сессией — её открывает вызывающий. Источник про
   сессии знать не должен, значит их держит обёртка.
2. У `list_active` нет фильтра по городу: он отдаёт активные чаты всех
   городов сразу. Отрезаем город здесь, на стороне Python — чатов десятки,
   это дешевле, чем разъезжаться с веткой слоя `db`. SQL-фильтр появится в
   репозитории, когда чатов станет много.
3. Обход ограничен десятью чатами (spec-v2, 2.3), а фильтр по городу
   применяется ПОСЛЕ выборки. Значит из базы берём с запасом, иначе десять
   чатов Нячанга превратились бы в два: восемь мест заняли бы чужие города.

Сам класс `sniffer.db` не импортирует: репозиторий и фабрика сессии приходят
снаружи, поэтому обёртка тестируется без базы. Знание о слое `db` собрано в
одной функции `new_directory` — это тот самый боевой реестр, который получает
адаптер, когда его создают из реестра источников без аргументов. Импорт внутри
функции, чтобы модуль остался импортируемым там, где базы нет вовсе.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.sources.telegram_reference import ChatDirectory, ChatLike

log = structlog.get_logger(__name__)

# Во сколько раз больше чатов просим у базы, чем нужно источнику. Города в
# реестре перемешаны, и без запаса фильтр оставил бы от выборки огрызок.
CITY_OVERFETCH = 5


class CityChatLike(ChatLike, Protocol):
    """Запись реестра с городом: его знает база, но не знает источник."""

    @property
    def city(self) -> str: ...


class ChatRecords(Protocol):
    """`ChatRepository` в той части, которой пользуется обёртка."""

    async def list_active(self, *, limit: int = ...) -> Sequence[CityChatLike]: ...


class EmptyChatDirectory:
    """Реестр отключён явно: чатов нет.

    Пустой реестр — это «искать негде», а не поломка, поэтому источник молча
    отдаёт пустой список и остаётся в плане.

    Заглушку теперь подставляют руками — тесты и инструменты, которым база не
    нужна. Значением по умолчанию она была ровно один раз и стоила того, что
    боевой поиск ходил по пустому списку, пока документация и тесты уверяли в
    обратном: `get_source("telegram_groups")` аргументов не передаёт, а
    источник получал заглушку и честно находил ноль.
    """

    async def list_active(self, *, city: str, limit: int) -> Sequence[ChatLike]:
        log.warning("telegram.no_chat_directory", city=city)
        return []


class RepositoryChatDirectory:
    """Реестр поверх `ChatRepository`: сессия, фильтр по городу, обрезка.

    Собирается там, где сходятся оба слоя, одной строкой:

        RepositoryChatDirectory(session_scope, ChatRepository)

    Оба аргумента — то, что уже есть в `sniffer.db`: `session_scope` даёт
    сессию на единицу работы, `ChatRepository` принимает её конструктором.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        repository_factory: Callable[[AsyncSession], ChatRecords],
        *,
        overfetch: int = CITY_OVERFETCH,
    ) -> None:
        self._session_factory = session_factory
        self._repository_factory = repository_factory
        self._overfetch = overfetch

    async def list_active(self, *, city: str, limit: int) -> Sequence[ChatLike]:
        asked = max(limit, 1) * self._overfetch
        # Читаем и ничего не меняем — коммит не нужен, сессия закрывается
        # выходом из `async with` (граница транзакции за вызывающим).
        async with self._session_factory() as session:
            found = await self._repository_factory(session).list_active(limit=asked)
        # Пустой город в записи означает «не указан» и поиск не сужает.
        of_city = [chat for chat in found if chat.city in ("", city)]
        if len(of_city) < limit and len(found) >= asked:
            # Запас упёрся в потолок: база отдала ровно столько, сколько
            # просили, значит за обрезкой остались чаты, среди которых мог быть
            # нужный город. Пора за SQL-фильтром.
            #
            # Условие требует ОБА признака. «Города мало» само по себе значит
            # только, что чатов этого города в реестре мало, — и на живой базе
            # (4 чата из 44 при запросе 50) это давало ложную тревогу о том,
            # чего не было: ничего не отрезано, спасать нечего.
            log.info(
                "telegram.city_overfetch_tight",
                city=city,
                got=len(of_city),
                raw=len(found),
                asked=asked,
            )
        return of_city[:limit]


def new_directory() -> ChatDirectory:
    """Боевой реестр чатов: таблица `chats` через `ChatRepository`.

    Та самая сборка из spec-v2 4.4 — и единственное место, где сходятся слой
    источников и слой доступа к данным. Именно её получает адаптер, созданный
    реестром источников без аргументов, то есть на боевом пути.
    """
    from sniffer.db import ChatRepository, session_scope

    return RepositoryChatDirectory(session_scope, ChatRepository)
