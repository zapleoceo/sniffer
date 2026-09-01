"""Единственное место, где разведка касается настоящего Telethon.

Отделено от `telegram_discover.py` намеренно: «как позвать Telegram» и «кого и
когда звать» меняются по разным поводам, а тесты второго не должны тащить за
собой первое. Разбор ответа вынесен ещё дальше — в
`telegram_discover_convert.py`: он проверяется таблицей значений и без сети.

Здесь же видны семь явных TL-запросов: четыре чтения и три на два действия:
`ResolveUsername`, `GetFullChannel`, `CheckChatInvite`, `contacts.Search`,
`JoinChannel`, `ImportChatInvite`, `UpdateNotifySettings`. Список закрытый
(CLAUDE.md, «Работа с Telegram»); третьего **действия** не появляется без
решения владельца, а `CheckChatInvite` — чтение: оно ничего не отправляет и в
чате не видно. История читается отдельным высокоуровневым `get_messages`, без
отметки о прочтении.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

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

    async def history(
        self, entity: int | str, *, limit: int, min_id: int = 0, max_id: int = 0
    ) -> Sequence[Any]:
        """Окно истории без отметки о прочтении.

        `get_messages` — чтение. В отличие от `send_read_acknowledge` оно не
        меняет состояние диалога у аккаунта и никому не видно.

        Два курсора, потому что лента читается в обе стороны: `min_id` даёт
        новое сверху (догон свежих постов), `max_id` — старое снизу (добор
        архива). Ноль у обоих означает «без границы» — так их понимает сам
        Telegram, и своей трактовки мы не выдумываем.
        """
        messages = await self._client.get_messages(
            entity, limit=limit, min_id=min_id, max_id=max_id
        )
        return cast(Sequence[Any], messages)

    async def join_public(self, username: str) -> int:
        """Исключение 1 из «юзербот только читает» (CLAUDE.md)."""
        from telethon.tl.functions.channels import JoinChannelRequest

        result = await self._client(JoinChannelRequest(channel=username.lstrip("@")))
        chat = first_chat(result)
        if chat is not None:
            return marked_id(chat)
        # Ответ без чата — это НЕ «вступления не было». Telegram отдаёт
        # `UpdatesTooLong` вместо списка обновлений, когда очередь обновлений
        # аккаунта переполнена, и чата в таком ответе нет вовсе. Запрос при
        # этом состоялся, аккаунт в группе, слот потрачен.
        #
        # Раньше здесь сразу летел `LookupError`, то есть «исход неизвестен», а
        # неизвестный исход возвращает кандидата в очередь — и через час уходил
        # ВТОРОЙ join в тот же чат. Замер на боевом аккаунте 01.09.2026: два
        # слота из десяти на @auto_moto_vietnam за сутки, реестр пуст, чат не
        # заглушен, очередь из 35 кандидатов стоит за первым.
        return await self._id_of_joined(username, answer=type(result).__name__)

    async def join_invite(self, invite_hash: str) -> int:
        """Та же дверь, что и `join_public`, только для закрытой группы."""
        from telethon.tl.functions.messages import ImportChatInviteRequest

        result = await self._client(ImportChatInviteRequest(hash=invite_hash))
        chat = first_chat(result)
        if chat is not None:
            return marked_id(chat)
        # Та же дыра и то же лечение, что в `join_public`. Имени у закрытой
        # группы нет, зато есть приглашение: после вступления `CheckChatInvite`
        # отдаёт `ChatInviteAlready` — с чатом и его id. Это чтение, слота оно
        # не стоит.
        log.warning("discover.join_without_chat", answer=type(result).__name__, invite=True)
        resolved = await self.check_invite(invite_hash)
        if resolved is None or not resolved.already_member or not resolved.tg_id:
            # `ChatInvite` без признака участника означает, что внутрь мы так и
            # не попали. Вот теперь исход действительно неизвестен.
            raise LookupError("вступили по приглашению, но чат не вернулся и не читается")
        return resolved.tg_id

    async def _id_of_joined(self, username: str, *, answer: str) -> int:
        """Id чата, в который только что вступили, — чтением, а не догадкой.

        `ResolveUsername` ничего не отправляет и в чате не виден, поэтому
        добор id не тратит ни суточный слот, ни право на действие: закрытый
        список CLAUDE.md ограничивает действия, а это чтение.
        """
        log.warning("discover.join_without_chat", username=username, answer=answer)
        resolved = await self.resolve_username(username)
        if resolved is None or resolved.is_user or not resolved.tg_id:
            # Имя перестало разрешаться или указывает на человека: вступать
            # было некуда, и придумывать id не из чего.
            raise LookupError(f"вступили в {username}, но чат не вернулся и не читается")
        return resolved.tg_id

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
