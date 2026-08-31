"""Telegram-группы — главный источник продукта.

Русскоязычный рынок Нячанга живёт в группах: там объявление появляется раньше,
чем на любой доске, а вьетнамский сегмент в Telegram отсутствует как явление
(spec-v2, 7) — за ним ходит Chotot. Ищем `messages.search` по чатам, в которых
юзербот уже состоит; вступление в группы остаётся ручной операцией владельца.

Четыре вещи, которые определяют этот адаптер:

- **Только чтение.** Клиент виден адаптеру через `TelegramReader`
  (`telegram_reference`): ни отправки, ни реакции, ни отметки о прочтении в
  этом типе нет, и mypy их не пропустит. Граница держит код, который ходит
  через протокол; голый Telethon её обходит (он без `py.typed`, для mypy это
  `Any`), поэтому вторая половина защиты — тест, который ищет исходящие вызовы
  по всем модулям пути чтения. Основание: spec-v2 6.1, аккаунт с `PEER_FLOOD`.
- **Бюджет.** Не больше десяти чатов на поиск (spec-v2, 2.3) и последовательный
  обход: параллелить запросы к одному хосту значит выглядеть как атака. Свои
  40 с из 90 с плана — на ВСЕ задачи источника, а не на каждую.
- **FloodWait — пауза, а не ретрай.** Пауза растёт, попыток на чат две. Если
  ждать дольше остатка бюджета, источник помечает себя `degraded` и выбывает
  из плана: остальные источники доигрывают, клиент простоя не замечает.
- **Мёртвая сессия — не про чат.** Отозванную сессию видно на первом же
  запросе, и остальные девять чатов её не воскресят: обход прекращается сразу.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from time import monotonic
from typing import Any

import structlog
from telethon.errors import AuthKeyError, FloodWaitError, UnauthorizedError

from sniffer.config import Settings, get_settings
from sniffer.sources.base import RawItem, Source, register
from sniffer.sources.chat_directory import new_directory
from sniffer.sources.telegram_client import missing_reader_settings, new_reader
from sniffer.sources.telegram_mapping import album_key, chats_limit, messages_limit, to_item
from sniffer.sources.telegram_reference import (
    FLOOD_BACKOFF_BASE,
    MAX_ATTEMPTS_PER_CHAT,
    SEARCH_BUDGET_S,
    SOURCE_NAME,
    ChatDirectory,
    ChatLike,
    TelegramReader,
)

log = structlog.get_logger(__name__)

ReaderFactory = Callable[[Settings], TelegramReader]
DirectoryFactory = Callable[[], ChatDirectory]
Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]

# Сессия отозвана, аккаунт удалён, ключ поднят с двух машин. Это состояние
# аккаунта, а не чата: следующие девять запросов дадут ту же ошибку.
SESSION_DEAD = (UnauthorizedError, AuthKeyError)


@register
class TelegramGroupsSource(Source):
    name = SOURCE_NAME

    def __init__(
        self,
        directory: ChatDirectory | None = None,
        client: TelegramReader | None = None,
        *,
        budget_s: float = SEARCH_BUDGET_S,
        reader_factory: ReaderFactory = new_reader,
        directory_factory: DirectoryFactory = new_directory,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = monotonic,
    ) -> None:
        super().__init__()
        # По умолчанию — боевой реестр, а не заглушка: адаптер создаётся
        # реестром источников без аргументов (`get_source(name)`), и всё, что
        # не подставлено по умолчанию, на боевом пути не появится никогда.
        # Фабрика, а не готовый объект: значение по умолчанию вычисляется один
        # раз при импорте модуля, и тогда `sniffer.db` подтянулся бы даже там,
        # где базы нет.
        self._directory = directory or directory_factory()
        self._client = client
        self._owns_client = client is None
        self._budget_s = budget_s
        self._reader_factory = reader_factory
        self._sleep = sleep
        self._clock = clock
        self._connected = False
        # Бюджет и счётчик флудов живут на адаптере, а не на вызове: план
        # вправе поставить до пяти задач на источник (spec-v2, 2.3), и делят
        # они одни и те же 40 с и одну и ту же немилость Telegram.
        self._deadline: float | None = None
        self._floods = 0
        # Альбомы, уже отданные наружу: у пяти фото с одной подписью разные
        # id, и дедуп по external_id их не схлопнет.
        self._albums: set[tuple[int, int]] = set()

    async def aclose(self) -> None:
        if self._client is not None and self._connected and self._owns_client:
            await self._client.disconnect()
            self._connected = False

    async def search(self, query: str, params: dict[str, Any]) -> list[RawItem]:
        text = query.strip()
        if not text:
            # Пустой запрос в messages.search вернёт всю группу подряд —
            # это не поиск, а выкачивание чата мимо воронки.
            log.warning("telegram.empty_query")
            return []

        chats = await self._chats(params)
        if not chats:
            return []
        client = await self._reader()
        if client is None:
            return []

        limit = messages_limit(params)
        items: list[RawItem] = []
        failed = 0
        for chat in chats:
            if self._left() <= 0:
                log.warning("telegram.budget_exceeded", budget_s=self._budget_s, done=len(items))
                break
            found = await self._from_chat(client, chat, text, limit)
            if found is None:
                failed += 1
            else:
                items.extend(found)
            if self.degraded:
                break

        if failed and failed == len(chats):
            # Ни один чат не ответил — это не «объявлений нет», это сломанный
            # источник, и в следующий план он идти не должен.
            self.degraded = True
            log.warning("telegram.all_chats_failed", chats=len(chats))
        log.info("telegram.search", query=text, chats=len(chats), found=len(items), failed=failed)
        return items

    def _left(self) -> float:
        """Сколько секунд бюджета осталось источнику. Отсчёт с первой задачи."""
        if self._deadline is None:
            self._deadline = self._clock() + self._budget_s
        return self._deadline - self._clock()

    async def _chats(self, params: dict[str, Any]) -> list[ChatLike]:
        city = str(params.get("city") or get_settings().default_city)
        limit = chats_limit(params)
        try:
            found = await self._directory.list_active(city=city, limit=limit)
        except Exception as exc:
            # Реестр — чужой слой, и без него искать негде. Падать нельзя:
            # контракт источника запрещает бросать наружу.
            self.degraded = True
            log.warning("telegram.directory_failed", error=f"{type(exc).__name__}: {exc}")
            return []
        # Сортировка своя, а не на доверии: обходим первые `limit` чатов, и
        # реестр, забывший про порядок, стоил бы клиенту самых плотных групп.
        usable = [chat for chat in sorted(found, key=lambda chat: chat.search_rank) if _valid(chat)]
        return usable[:limit]

    async def _reader(self) -> TelegramReader | None:
        if self._client is None:
            settings = get_settings()
            missing = missing_reader_settings(settings)
            if missing:
                self.degraded = True
                log.warning("telegram.not_configured", missing=missing)
                return None
            self._client = self._reader_factory(settings)
        if self._connected:
            return self._client
        try:
            await self._client.connect()
        except Exception as exc:
            self.degraded = True
            log.warning("telegram.connect_failed", error=f"{type(exc).__name__}: {exc}")
            return None
        self._connected = True
        return self._client

    async def _from_chat(
        self,
        client: TelegramReader,
        chat: ChatLike,
        query: str,
        limit: int,
    ) -> list[RawItem] | None:
        """Находки одного чата либо `None`, если чат не ответил."""
        # username, а не id: по имени сущность разрешается с холодной сессии,
        # по голому id — только если чат уже лежит в кэше сессии.
        entity: int | str = chat.username or chat.tg_id
        for attempt in range(MAX_ATTEMPTS_PER_CHAT):
            try:
                messages = await client.get_messages(entity, search=query, limit=limit)
            except FloodWaitError as flood:
                # Флуд считаем всегда: лимит у Telegram на аккаунт, а не на
                # чат, и следующему чату эта немилость достанется целиком.
                self._floods += 1
                pause = self._pause(flood)
                if not self._fits(pause, chat):
                    # Telegram просит больше времени, чем у источника осталось:
                    # это состояние аккаунта, и обход прекращается целиком.
                    return None
                if attempt == MAX_ATTEMPTS_PER_CHAT - 1:
                    # Попыток больше нет, и пауза перед несуществующей попыткой
                    # — выброшенный бюджет: чат всё равно бросаем, а остальные
                    # чаты это время не получат.
                    log.warning(
                        "telegram.chat_flooded_out",
                        chat=chat.tg_id,
                        floods=self._floods,
                        skipped_pause_s=pause,
                    )
                    return None
                log.warning(
                    "telegram.flood_wait", chat=chat.tg_id, pause_s=pause, floods=self._floods
                )
                await self._sleep(pause)
                continue
            except SESSION_DEAD as dead:
                # Дальше идти некуда: сессию чинит владелец командой auth, а
                # не девять одинаковых отказов подряд.
                self.degraded = True
                log.error("telegram.session_dead", error=f"{type(dead).__name__}: {dead}")
                return None
            except Exception as exc:
                # Чат мог быть покинут, удалён или переименован — это потеря
                # одного чата, а не источника.
                log.warning(
                    "telegram.chat_failed",
                    chat=chat.tg_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                return None
            return self._items(chat, messages)
        # Недостижимо: последняя попытка возвращает результат либо `None` выше.
        return None

    def _items(self, chat: ChatLike, messages: Sequence[Any]) -> list[RawItem]:
        """Находки чата без повторов одного альбома."""
        items: list[RawItem] = []
        for message in messages:
            key = album_key(chat, message)
            if key is not None and key in self._albums:
                continue
            item = to_item(chat, message)
            if item is None:
                continue
            if key is not None:
                self._albums.add(key)
            items.append(item)
        return items

    def _pause(self, flood: FloodWaitError) -> float:
        """Сколько ждать после этого FloodWait.

        Первый флуд ждёт ровно столько, сколько назвал Telegram, — названный
        минимум мы уважаем. Дальше пауза удваивается: повтор подряд означает,
        что минимума мало.
        """
        wait_s = max(float(getattr(flood, "seconds", 0) or 0), 1.0)
        return wait_s * FLOOD_BACKOFF_BASE ** (self._floods - 1)

    def _fits(self, pause: float, chat: ChatLike) -> bool:
        """Помещается ли пауза в остаток бюджета. `False` — источник выбывает.

        Проверяется и на последней попытке, где ждать мы всё равно не станем:
        «Telegram просит больше, чем у источника осталось» — это про аккаунт, а
        не про чат, и следующий запрос его только усугубит.
        """
        remaining = self._left()
        if pause <= remaining:
            return True
        self.degraded = True
        log.warning(
            "telegram.flood_over_budget",
            chat=chat.tg_id,
            pause_s=pause,
            remaining_s=remaining,
        )
        return False


def _valid(chat: ChatLike) -> bool:
    """Запись реестра, которой можно пользоваться.

    Положительный `tg_id` — это id ПОЛЬЗОВАТЕЛЯ: Telethon пойдёт искать в
    личной переписке, а ссылка уедет на чужой чат, и всё это молча. У группы
    id отрицательный всегда, поэтому положительный означает битую строку
    реестра — и она стоит громкой ошибки, а не попытки.
    """
    if chat.tg_id < 0:
        return True
    log.error("telegram.chat_id_not_marked", chat=chat.tg_id, title=chat.title)
    return False
