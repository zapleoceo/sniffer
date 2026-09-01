"""Единственное место, где разведка касается настоящего Telethon.

Отделено от `telegram_discover.py` намеренно: «как позвать Telegram» и «кого и
когда звать» меняются по разным поводам, а тесты второго не должны тащить за
собой первое. Разбор ответа вынесен ещё дальше — в
`telegram_discover_convert.py`: он проверяется таблицей значений и без сети.

Здесь же видна вся поверхность целиком — семь запросов: четыре чтения
и три на два действия:
`ResolveUsername`, `GetFullChannel`, `CheckChatInvite`, `contacts.Search`,
`JoinChannel`, `ImportChatInvite`, `UpdateNotifySettings`. Список закрытый
(CLAUDE.md, «Работа с Telegram»); третьего **действия** не появляется без
решения владельца, а `CheckChatInvite` — чтение: оно ничего не отправляет и в
чате не видно.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog

from sniffer.config import Settings
from sniffer.sources.telegram_discover_convert import (
    first_chat,
    first_user,
    from_chat,
    from_invite,
    from_user,
    marked_id,
    with_details,
)
from sniffer.sources.telegram_discover_reference import ResolvedChat, TelegramJoiner
from sniffer.telegram_identity import IDENTITY

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
        chat = first_chat(result)
        if chat is None:
            user = first_user(result)
            return from_user(user) if user is not None else None
        resolved = from_chat(chat)
        return with_details(resolved, about=await self._about(chat))

    async def check_invite(self, invite_hash: str) -> ResolvedChat | None:
        """То же, что `resolve_username`, но для закрытой группы — и тоже чтение.

        `messages.checkChatInvite` отдаёт название, описание, тип и признак
        «только по заявке», не вступая. Без него вступление по приглашению
        уходило бы вслепую, а отменить его нечем: выйти из чата закрытый список
        CLAUDE.md не разрешает.
        """
        from telethon.tl.functions.messages import CheckChatInviteRequest

        result = await self._client(CheckChatInviteRequest(hash=invite_hash))
        chat = getattr(result, "chat", None)
        if chat is not None:
            # `ChatInviteAlready` / `ChatInvitePeek`: мы уже внутри либо нам дали
            # заглянуть. Id здесь есть, вступать больше некуда, и описание за
            # вторым запросом не идёт — отбор всё равно остановится на этом
            # признаке.
            return with_details(from_chat(chat), already_member=True)
        return from_invite(result)

    async def search_contacts(self, query: str, limit: int) -> Sequence[ResolvedChat]:
        """`contacts.Search` вместо перебора диалогов (spec-v2, 7)."""
        from telethon.tl.functions.contacts import SearchRequest

        result = await self._client(SearchRequest(q=query, limit=limit))
        return [from_chat(chat) for chat in getattr(result, "chats", ())]

    async def join_public(self, username: str) -> int:
        """Исключение 1 из «юзербот только читает» (CLAUDE.md)."""
        from telethon.tl.functions.channels import JoinChannelRequest

        result = await self._client(JoinChannelRequest(channel=username.lstrip("@")))
        chat = first_chat(result)
        if chat is None:
            raise LookupError(f"вступили в {username}, но чат не вернулся")
        return marked_id(chat)

    async def join_invite(self, invite_hash: str) -> int:
        """Та же дверь, что и `join_public`, только для закрытой группы."""
        from telethon.tl.functions.messages import ImportChatInviteRequest

        result = await self._client(ImportChatInviteRequest(hash=invite_hash))
        chat = first_chat(result)
        if chat is None:
            raise LookupError("вступили по приглашению, но чат не вернулся")
        return marked_id(chat)

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
        **IDENTITY,
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
