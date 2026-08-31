"""Сущность Telegram → запись отбора. Чистое преобразование, без сети.

Отделено от `telegram_discover_client.py`, потому что это разные знания: там
«как позвать Telegram», здесь «как понять, что он ответил». Второе проверяется
таблицей значений и меняется вместе со схемой Telegram, первое — вместе с
Telethon.

Ни одной функции с вводом-выводом здесь нет и быть не должно: модуль обязан
оставаться проверяемым без клиента и без сессии.
"""

from __future__ import annotations

from typing import Any

from sniffer.sources.telegram_discover_reference import ResolvedChat


def first_chat(result: Any) -> Any | None:
    chats = getattr(result, "chats", None) or ()
    return chats[0] if chats else None


def first_user(result: Any) -> Any | None:
    users = getattr(result, "users", None) or ()
    return users[0] if users else None


def marked_id(chat: Any) -> int:
    """Id в размеченной форме — той, что хранит реестр и ждёт `telegram_groups`.

    Считает Telethon, а не мы: у супергруппы разметка `-100` + id, у обычной
    группы просто `-id`, и своя арифметика на этом месте однажды уедет на
    чужой чат. Знание о форме id принадлежит библиотеке.
    """
    from telethon import utils

    return int(utils.get_peer_id(chat))


def is_group(entity: Any) -> bool:
    """Группа это или вещание. Одна функция на `Channel`, `Chat` и `ChatInvite`.

    `megagroup` — то самое различие между «группой» и «каналом»: у канала он
    ложный, и объявлений от людей там не бывает (docs/chats-nha-trang.md).
    Обычный `Chat` (не супергруппа) канала не имеет вовсе — он группа по типу,
    и ни одного из этих флагов у него нет.

    `gigagroup` проверяется явно и раньше остальных. Это супергруппа, которую
    перевели в режим вещания: писать в ней вправе только администраторы, то есть
    объявлений от людей в ней ровно столько же, сколько в канале, — ноль.
    Выводить это из пары `broadcast`/`megagroup` нельзя: при обоих поднятых
    флагах вывод «раз megagroup, значит группа» давал канал, принятый за
    барахолку, а вступление отменить нечем.
    """
    if bool(getattr(entity, "gigagroup", False)):
        return False
    if bool(getattr(entity, "megagroup", False)):
        return True
    return not bool(getattr(entity, "broadcast", False))


def from_chat(chat: Any) -> ResolvedChat:
    """Сущность Telegram → запись отбора. Тип считает `is_group`."""
    return ResolvedChat(
        tg_id=marked_id(chat),
        username=str(getattr(chat, "username", "") or ""),
        title=str(getattr(chat, "title", "") or ""),
        is_group=is_group(chat),
        participants=int(getattr(chat, "participants_count", 0) or 0),
    )


def from_user(user: Any) -> ResolvedChat:
    return ResolvedChat(
        tg_id=int(getattr(user, "id", 0)),
        username=str(getattr(user, "username", "") or ""),
        title=str(getattr(user, "first_name", "") or ""),
        is_bot=bool(getattr(user, "bot", False)),
        is_user=True,
    )


def from_invite(invite: Any) -> ResolvedChat | None:
    """`ChatInvite` — чат, в котором мы ещё не состоим.

    `tg_id` остаётся нулевым намеренно: до вступления Telegram id закрытого чата
    не отдаёт, и выдумать его нечем. Отбор это учитывает — сверка с реестром по
    id делается только когда id есть.

    `username` пустой: у приглашения его нет. Отбор ищет город в названии и
    описании, и обоих здесь достаточно.
    """
    if getattr(invite, "title", None) is None:
        # Не `ChatInvite` вовсе: Telegram вернул что-то, чего мы не знаем. Лучше
        # отказ по «не разобрали», чем вступление вслепую.
        return None
    return ResolvedChat(
        tg_id=0,
        username="",
        title=str(getattr(invite, "title", "") or ""),
        about=str(getattr(invite, "about", "") or ""),
        is_group=is_group(invite),
        participants=int(getattr(invite, "participants_count", 0) or 0),
        request_needed=bool(getattr(invite, "request_needed", False)),
    )


def with_details(
    chat: ResolvedChat, *, about: str = "", already_member: bool = False
) -> ResolvedChat:
    """Достроить запись: описание приходит вторым запросом, признак — из формы."""
    return ResolvedChat(
        tg_id=chat.tg_id,
        username=chat.username,
        title=chat.title,
        about=about or chat.about,
        is_group=chat.is_group,
        is_bot=chat.is_bot,
        is_user=chat.is_user,
        participants=chat.participants,
        request_needed=chat.request_needed,
        already_member=already_member or chat.already_member,
    )
