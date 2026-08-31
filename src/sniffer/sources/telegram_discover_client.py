"""Единственное место, где разведка касается настоящего Telethon.

Отделено от `telegram_discover.py` намеренно: «как позвать Telegram» и «кого и
когда звать» меняются по разным поводам, а тесты второго не должны тащить за
собой первое.

Здесь же видно всю поверхность целиком — четыре чтения и два действия:
`ResolveUsername`, `GetFullChannel`, `contacts.Search`, `JoinChannel`,
`ImportChatInvite`, `UpdateNotifySettings`. Список закрытый (CLAUDE.md,
«Работа с Telegram»); третьего действия не появляется без решения владельца.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog

from sniffer.config import Settings
from sniffer.sources.telegram_discover_reference import ResolvedChat, TelegramJoiner

log = structlog.get_logger(__name__)

# Беззвучный режим ставится «навсегда»: Telegram считает временем снятия
# заглушки эту метку, и максимальный int означает «не снимать».
MUTE_FOREVER = 2**31 - 1


class TelethonJoiner:
    """Telethon в объёме протокола `TelegramJoiner` и ни методом больше.

    Обёртка, а не голый клиент: у `TelegramClient` есть и `send_message`, и
    `forward_messages`, и всё остальное запрещённое. Отдать его наружу значило
    бы понадеяться на дисциплину вызывающего; здесь запрет держит тип.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def connect(self) -> None:
        await self._client.connect()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    async def resolve_username(self, username: str) -> ResolvedChat | None:
        """Тип, название и описание — не вступая. Ради этого разведка и есть."""
        from telethon.tl.functions.contacts import ResolveUsernameRequest

        result = await self._client(ResolveUsernameRequest(username=username.lstrip("@")))
        chat = _first_chat(result)
        if chat is None:
            user = _first_user(result)
            return _from_user(user) if user is not None else None
        resolved = _from_chat(chat)
        return _with_about(resolved, await self._about(chat))

    async def search_contacts(self, query: str, limit: int) -> Sequence[ResolvedChat]:
        """`contacts.Search` вместо перебора диалогов (spec-v2, 7)."""
        from telethon.tl.functions.contacts import SearchRequest

        result = await self._client(SearchRequest(q=query, limit=limit))
        return [_from_chat(chat) for chat in getattr(result, "chats", ())]

    async def join_public(self, username: str) -> int:
        """Исключение 1 из «юзербот только читает» (CLAUDE.md)."""
        from telethon.tl.functions.channels import JoinChannelRequest

        result = await self._client(JoinChannelRequest(channel=username.lstrip("@")))
        chat = _first_chat(result)
        if chat is None:
            raise LookupError(f"вступили в {username}, но чат не вернулся")
        return _marked_id(chat)

    async def join_invite(self, invite_hash: str) -> int:
        """Та же дверь, что и `join_public`, только для закрытой группы."""
        from telethon.tl.functions.messages import ImportChatInviteRequest

        result = await self._client(ImportChatInviteRequest(hash=invite_hash))
        chat = _first_chat(result)
        if chat is None:
            raise LookupError("вступили по приглашению, но чат не вернулся")
        return _marked_id(chat)

    async def set_muted(self, tg_id: int) -> None:
        """Исключение 2: аккаунт рабочий, и два десятка барахолок его хоронят."""
        from telethon.tl.functions.account import UpdateNotifySettingsRequest
        from telethon.tl.types import InputNotifyPeer, InputPeerNotifySettings

        peer = await self._client.get_input_entity(tg_id)
        await self._client(
            UpdateNotifySettingsRequest(
                peer=InputNotifyPeer(peer),
                settings=InputPeerNotifySettings(mute_until=MUTE_FOREVER, show_previews=False),
            )
        )

    async def _about(self, chat: Any) -> str:
        """Описание живёт в `GetFullChannel` — это чтение, не действие."""
        from telethon.tl.functions.channels import GetFullChannelRequest

        try:
            full = await self._client(GetFullChannelRequest(channel=chat))
        except Exception as exc:
            # Описания может не быть или его могут не отдать. Отбор обойдётся
            # названием: пустое описание хуже, чем упавшая разведка.
            log.info("discover.about_unavailable", error=f"{type(exc).__name__}: {exc}")
            return ""
        return str(getattr(getattr(full, "full_chat", None), "about", "") or "")


def new_joiner(settings: Settings) -> TelegramJoiner:
    """Импорт внутри функции: тестам Telethon не нужен вовсе.

    `flood_sleep_threshold=0` обязателен. По умолчанию Telethon сам спит на
    `FloodWait` до минуты внутри вызова и повторяет запрос, не сказав ни слова
    наружу — а на join повтор запрещён: флуд там предупреждение, а не задержка,
    и остановиться до конца суток мы обязаны сами.
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(
        StringSession(settings.tg_session),
        settings.tg_api_id,
        settings.tg_api_hash,
        flood_sleep_threshold=0,
    )
    return TelethonJoiner(client)


def missing_joiner_settings(settings: Settings) -> list[str]:
    """Имена пустых настроек так, как они названы в `.env`."""
    required = {
        "TG_API_ID": bool(settings.tg_api_id),
        "TG_API_HASH": bool(settings.tg_api_hash.strip()),
        "TG_SESSION": bool(settings.tg_session.strip()),
    }
    return [name for name, filled in required.items() if not filled]


def _first_chat(result: Any) -> Any | None:
    chats = getattr(result, "chats", None) or ()
    return chats[0] if chats else None


def _first_user(result: Any) -> Any | None:
    users = getattr(result, "users", None) or ()
    return users[0] if users else None


def _marked_id(chat: Any) -> int:
    """Id в размеченной форме — той, что хранит реестр и ждёт `telegram_groups`.

    Считает Telethon, а не мы: у супергруппы разметка `-100` + id, у обычной
    группы просто `-id`, и своя арифметика на этом месте однажды уедет на
    чужой чат. Знание о форме id принадлежит библиотеке.
    """
    from telethon import utils

    return int(utils.get_peer_id(chat))


def _from_chat(chat: Any) -> ResolvedChat:
    """Сущность Telegram → запись отбора.

    `megagroup` — то самое различие между «группой» и «каналом»: у канала он
    ложный, и объявлений от людей там не бывает (docs/chats-nha-trang.md).
    Обычный `Chat` (не супергруппа) канала не имеет вовсе — он группа по типу.
    """
    is_broadcast = bool(getattr(chat, "broadcast", False))
    is_megagroup = bool(getattr(chat, "megagroup", False))
    return ResolvedChat(
        tg_id=_marked_id(chat),
        username=str(getattr(chat, "username", "") or ""),
        title=str(getattr(chat, "title", "") or ""),
        is_group=is_megagroup or not is_broadcast,
        participants=int(getattr(chat, "participants_count", 0) or 0),
    )


def _from_user(user: Any) -> ResolvedChat:
    return ResolvedChat(
        tg_id=int(getattr(user, "id", 0)),
        username=str(getattr(user, "username", "") or ""),
        title=str(getattr(user, "first_name", "") or ""),
        is_bot=bool(getattr(user, "bot", False)),
        is_user=True,
    )


def _with_about(chat: ResolvedChat, about: str) -> ResolvedChat:
    return ResolvedChat(
        tg_id=chat.tg_id,
        username=chat.username,
        title=chat.title,
        about=about,
        is_group=chat.is_group,
        is_bot=chat.is_bot,
        is_user=chat.is_user,
        participants=chat.participants,
    )
