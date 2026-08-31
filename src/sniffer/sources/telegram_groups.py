"""Telegram-группы — главный источник продукта.

Русскоязычный рынок Нячанга живёт в группах: там объявление появляется раньше,
чем на любой доске, а вьетнамский сегмент в Telegram отсутствует как явление
(spec-v2, 7) — за ним ходит Chotot. Ищем `messages.search` по чатам, в которых
юзербот уже состоит; вступление в группы остаётся ручной операцией владельца.

Три вещи, которые определяют этот адаптер:

- **Только чтение.** Разрешённые методы перечислены в `TelegramReader`
  (`telegram_reference`), и других у клиента нет — ни отправки, ни реакции,
  ни отметки о прочтении. Основание: spec-v2 6.1, аккаунт с `PEER_FLOOD`.
- **Бюджет.** Не больше десяти чатов на поиск (spec-v2, 2.3) и последовательный
  обход: параллелить запросы к одному хосту значит выглядеть как атака.
- **FloodWait — пауза, а не ретрай.** Пауза растёт, попыток на чат две. Если
  ждать дольше остатка бюджета, источник помечает себя `degraded` и выбывает
  из плана: остальные источники доигрывают, клиент простоя не замечает.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any

import structlog
from telethon.errors import FloodWaitError

from sniffer.config import Settings, get_settings
from sniffer.sources.base import RawItem, Source, register
from sniffer.sources.telegram_client import (
    EmptyChatDirectory,
    missing_reader_settings,
    new_reader,
)
from sniffer.sources.telegram_reference import (
    DEFAULT_MESSAGES_PER_CHAT,
    FLOOD_BACKOFF_BASE,
    MAX_ATTEMPTS_PER_CHAT,
    MAX_CHATS_PER_SEARCH,
    MAX_MESSAGES_PER_CHAT,
    SEARCH_BUDGET_S,
    SOURCE_NAME,
    ChatDirectory,
    ChatRef,
    MessageLike,
    TelegramReader,
    message_link,
)

log = structlog.get_logger(__name__)

ReaderFactory = Callable[[Settings], TelegramReader]
Sleep = Callable[[float], Awaitable[None]]


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
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        super().__init__()
        self._directory = directory or EmptyChatDirectory()
        self._client = client
        self._owns_client = client is None
        self._budget_s = budget_s
        self._reader_factory = reader_factory
        self._sleep = sleep
        self._connected = False
        # Счётчик FloodWait живёт на адаптере, а не на вызове: несколько задач
        # плана идут одним источником, и второй флуд подряд обязан ждать
        # дольше первого, даже если это уже другой запрос.
        self._floods = 0

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

        deadline = monotonic() + self._budget_s
        limit = _messages_limit(params)
        items: list[RawItem] = []
        failed = 0
        for chat in chats:
            if monotonic() >= deadline:
                log.warning("telegram.budget_exceeded", budget_s=self._budget_s, done=len(items))
                break
            found = await self._from_chat(client, chat, text, limit, deadline)
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

    async def _chats(self, params: dict[str, Any]) -> list[ChatRef]:
        city = str(params.get("city") or get_settings().default_city)
        limit = _chats_limit(params)
        try:
            found = await self._directory.active_chats(city=city, limit=limit)
        except Exception as exc:
            # Реестр — чужой слой, и без него искать негде. Падать нельзя:
            # контракт источника запрещает бросать наружу.
            self.degraded = True
            log.warning("telegram.directory_failed", error=f"{type(exc).__name__}: {exc}")
            return []
        # Сортировка своя, а не на доверии: обходим первые `limit` чатов, и
        # реестр, забывший про порядок, стоил бы клиенту самых плотных групп.
        return sorted(found, key=lambda chat: chat.search_rank)[:limit]

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
        chat: ChatRef,
        query: str,
        limit: int,
        deadline: float,
    ) -> list[RawItem] | None:
        """Находки одного чата либо `None`, если чат не ответил."""
        # username, а не id: по имени сущность разрешается с холодной сессии,
        # по голому id — только если чат уже лежит в кэше сессии.
        entity: int | str = chat.username or chat.tg_id
        for _ in range(MAX_ATTEMPTS_PER_CHAT):
            try:
                messages = await client.get_messages(entity, search=query, limit=limit)
            except FloodWaitError as flood:
                if not await self._wait_out(flood, deadline, chat):
                    return None
                continue
            except Exception as exc:
                # Чат мог быть покинут, удалён или переименован — это потеря
                # одного чата, а не источника.
                log.warning(
                    "telegram.chat_failed",
                    chat=chat.tg_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                return None
            return [item for msg in messages if (item := _to_item(chat, msg)) is not None]
        log.warning("telegram.chat_flooded_out", chat=chat.tg_id)
        return None

    async def _wait_out(self, flood: FloodWaitError, deadline: float, chat: ChatRef) -> bool:
        """Переждать FloodWait. `False` — ждать дольше, чем осталось бюджета."""
        self._floods += 1
        wait_s = max(float(getattr(flood, "seconds", 0) or 0), 1.0)
        pause = wait_s * FLOOD_BACKOFF_BASE ** (self._floods - 1)
        remaining = deadline - monotonic()
        if pause > remaining:
            self.degraded = True
            log.warning(
                "telegram.flood_over_budget",
                chat=chat.tg_id,
                pause_s=pause,
                remaining_s=remaining,
            )
            return False
        log.warning("telegram.flood_wait", chat=chat.tg_id, pause_s=pause, floods=self._floods)
        await self._sleep(pause)
        return True


def _to_item(chat: ChatRef, message: MessageLike) -> RawItem | None:
    """Сообщение → находка. Чего в сообщении нет — того нет и в находке.

    Пустые `title`, `price_raw`, `seller_name` — не недоделка. Заголовка у
    поста в группе не бывает, цену вынимает воронка из текста, а автора поста
    API группы не отдаёт вовсе (spec-v2, 7): контакт берём из текста
    объявления, иначе у клиента остаётся ссылка на пост.
    """
    text = (message.message or "").strip()
    if not text:
        # Фото без подписи, вход участника, закрепление — не объявления.
        return None
    return RawItem(
        source=SOURCE_NAME,
        external_id=f"{chat.tg_id}:{message.id}",
        url=message_link(chat, message.id),
        text=text,
        # Даты нет — так и отдаём: проверка живости пометит лот «дата
        # неизвестна» (spec-v2, 3.3), а выдуманная дата её обманет.
        posted_at=message.date,
        raw={
            "chat_tg_id": chat.tg_id,
            "chat_title": chat.title,
            "msg_id": message.id,
            "has_media": message.media is not None,
        },
    )


def _chats_limit(params: dict[str, Any]) -> int:
    """Сколько чатов обойти. Потолок из spec-v2 2.3 понижать можно, поднимать нет."""
    wanted = _as_int(params.get("chat_limit")) or get_settings().live_search_max_chats
    return max(1, min(wanted, MAX_CHATS_PER_SEARCH))


def _messages_limit(params: dict[str, Any]) -> int:
    wanted = _as_int(params.get("limit")) or DEFAULT_MESSAGES_PER_CHAT
    return max(1, min(wanted, MAX_MESSAGES_PER_CHAT))


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except ValueError:
        return None
