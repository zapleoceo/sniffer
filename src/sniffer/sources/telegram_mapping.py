"""Сообщение Telegram → `RawItem` и разбор параметров задачи.

Отделено от самого поиска: «что считать находкой» меняется от того, что мы
узнаём про объявления, а «как ходить в Telegram» — от того, что мы узнаём про
Telethon. Поводы разные, файлы тоже.
"""

from __future__ import annotations

from typing import Any

from sniffer.config import get_settings
from sniffer.domain.prices import price_hint
from sniffer.sources.base import RawItem
from sniffer.sources.telegram_reference import (
    DEFAULT_MESSAGES_PER_CHAT,
    MAX_CHATS_PER_SEARCH,
    MAX_MESSAGES_PER_CHAT,
    SOURCE_NAME,
    ChatLike,
    MessageLike,
    message_link,
    topic_id,
)


def to_item(chat: ChatLike, message: MessageLike) -> RawItem | None:
    """Находка из сообщения. Чего в сообщении нет — того нет и в находке.

    Пустые `title`, `price_raw`, `seller_name` — не недоделка. Заголовка у
    поста в группе не бывает, цену вынимает воронка из текста, а автора поста
    API группы не отдаёт вовсе (spec-v2, 7): контакт берём из текста
    объявления, иначе у клиента остаётся ссылка на пост.
    """
    text = (message.message or "").strip()
    if not text:
        # Фото без подписи, вход участника, закрепление — не объявления.
        return None
    price_raw, price_vnd = price_hint(text)
    return RawItem(
        source=SOURCE_NAME,
        external_id=f"{chat.tg_id}:{message.id}",
        url=message_link(chat, message.id, topic_id(message)),
        text=text,
        price_raw=price_raw,
        price_vnd=price_vnd,
        # Даты нет — так и отдаём: проверка живости пометит лот «дата
        # неизвестна» (spec-v2, 3.3), а выдуманная дата её обманет.
        posted_at=message.date,
        raw={
            "chat_tg_id": chat.tg_id,
            "chat_title": chat.title,
            "msg_id": message.id,
            "has_media": message.media is not None,
            # Альбом: воронке пригодится, чтобы собрать фото объявления
            # обратно в одну карточку.
            "grouped_id": message.grouped_id,
            "topic_id": topic_id(message),
        },
    )


def album_key(chat: ChatLike, message: MessageLike) -> tuple[int, int] | None:
    """Ключ медиагруппы или `None`, если сообщение одиночное.

    Пять фото с одной подписью приезжают пятью сообщениями с разными `id` и
    одинаковым `grouped_id`. Дедуп по `(source, external_id)` их не поймает —
    id-то разные, — и клиент увидит одно объявление пять раз подряд.
    """
    grouped = message.grouped_id
    return None if grouped is None else (chat.tg_id, grouped)


def chats_limit(params: dict[str, Any]) -> int:
    """Сколько чатов обойти. Потолок из spec-v2 2.3 понижать можно, поднимать нет."""
    wanted = as_int(params.get("chat_limit")) or get_settings().live_search_max_chats
    return max(1, min(wanted, MAX_CHATS_PER_SEARCH))


def messages_limit(params: dict[str, Any]) -> int:
    wanted = as_int(params.get("limit")) or DEFAULT_MESSAGES_PER_CHAT
    return max(1, min(wanted, MAX_MESSAGES_PER_CHAT))


def as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except ValueError:
        return None
