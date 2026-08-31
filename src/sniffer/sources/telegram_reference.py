"""Контракт адаптера telegram_groups: константы, протоколы, ссылка на пост.

Отдельно от адаптера по двум причинам. Первая — протоколы здесь нужны трём
сторонам сразу: адаптеру, обёртке над репозиторием чатов и тестам. Вторая —
`ChatDirectory` объявлен протоколом намеренно: реестр чатов живёт в таблице
`chats`, но адаптер не имеет права ни знать про SQL, ни ждать, пока слой `db`
будет написан. Реализация подставляется снаружи, структурная типизация не
требует от `db` импортировать этот модуль — обратной зависимости не возникает.

Разрешённая поверхность Telegram сведена к `TelegramReader`. Юзербот только
читает (CLAUDE.md, spec-v2 6.1), и «только» здесь означает: методов, которыми
можно что-то отправить, отметить прочитанным или вступить в группу, в типе
клиента просто нет. Дописать отправку молча не выйдет — mypy не пропустит.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

SOURCE_NAME = "telegram_groups"

# spec-v2, 2.3: не больше 10 чатов на один messages.search, иначе FloodWait.
# Потолок жёсткий: настройка может его понизить, но не поднять.
MAX_CHATS_PER_SEARCH = 10

# Сообщений с одного чата за запрос. Telethon режет выдачу на пачки по 100 и
# на каждую делает отдельный RPC, поэтому больше сотни — это тихое умножение
# числа запросов, то есть ровно то, за что прилетает FloodWait.
DEFAULT_MESSAGES_PER_CHAT = 30
MAX_MESSAGES_PER_CHAT = 100

# Доля источника от 90 с на весь план (spec-v2, 2.3) — на ВСЕ его задачи, а не
# на каждую: планировщик вправе поставить до пяти задач на источник, и по 40 с
# на каждую они съели бы бюджет остальных источников целиком.
SEARCH_BUDGET_S = 40.0

# Пауза после FloodWait растёт: Telegram называет минимум, а повтор подряд
# означает, что минимума мало. Ретрая в цикле нет — попыток на чат две.
FLOOD_BACKOFF_BASE = 2.0
MAX_ATTEMPTS_PER_CHAT = 2

# Префикс, которым Telegram размечает id супергрупп и каналов: -100 + id.
CHANNEL_ID_PREFIX = "100"


class ChatLike(Protocol):
    """Чат из реестра в той части, которой пользуется адаптер.

    Протокол, а не наш класс: репозиторий отдаёт `sniffer.domain.records.Chat`
    с дюжиной полей, и требовать от него преобразования в чужой тип значило бы
    править ветку слоя `db`.

    `tg_id` — размеченная форма, та самая, что отдаёт Telethon в
    `message.chat_id`: у супергруппы это `-100` + внутренний id. Форма важна:
    голое положительное число Telethon примет за id пользователя, поэтому
    адаптер такую запись реестра отвергает, а не «пробует».

    `search_rank` — порядок обхода, **меньший идёт раньше** (так и сортирует
    `ChatRepository.list_active`). Обходим не весь реестр, а первые
    `MAX_CHATS_PER_SEARCH`, поэтому порядок решает, что клиент вообще увидит.
    """

    @property
    def tg_id(self) -> int: ...

    @property
    def title(self) -> str: ...

    @property
    def username(self) -> str | None: ...

    @property
    def search_rank(self) -> int: ...


@dataclass(frozen=True, slots=True)
class ChatRef:
    """Минимальный чат: фикстуры, тесты и всё, что живёт без базы."""

    tg_id: int
    title: str = ""
    username: str | None = None
    search_rank: int = 100


class ChatDirectory(Protocol):
    """Реестр чатов: откуда адаптер узнаёт, где искать.

    Форма повторяет `ChatRepository.list_active` из слоя `db` — то же имя, тот
    же ключевой `limit`, тот же тип записей. Отличие ровно одно: город.
    Репозиторий фильтра по городу не имеет и отдаёт активные чаты всех
    городов сразу, поэтому город отрезается в обёртке
    (`sources/chat_directory.py`), а не выдумывается здесь.

    Адаптер всё равно обрежет список до бюджета сам: реализация, забывшая про
    `limit`, не должна превращаться во FloodWait.
    """

    async def list_active(self, *, city: str, limit: int) -> Sequence[ChatLike]: ...


class ReplyHeaderLike(Protocol):
    """`message.reply_to` в той части, по которой опознаётся тема форума."""

    @property
    def forum_topic(self) -> bool: ...

    @property
    def reply_to_msg_id(self) -> int | None: ...

    @property
    def reply_to_top_id(self) -> int | None: ...


class MessageLike(Protocol):
    """Сообщение Telegram в той части, которую читает адаптер.

    Свойства, а не поля: изменяемый атрибут в протоколе инвариантен, и тогда
    ни один класс с уточнённым типом поля в протокол не укладывается.
    """

    @property
    def id(self) -> int: ...

    @property
    def message(self) -> str | None: ...

    @property
    def date(self) -> datetime | None: ...

    @property
    def media(self) -> object | None: ...

    @property
    def grouped_id(self) -> int | None: ...

    @property
    def reply_to(self) -> ReplyHeaderLike | None: ...


class TelegramReader(Protocol):
    """Telethon в объёме «подключиться, поискать, отключиться». Больше — нельзя.

    Ни `send_message`, ни `join_channel`, ни `send_read_acknowledge` сюда не
    попадают, и это не забывчивость: аккаунт ловит `PEER_FLOOD` за исходящие
    (spec-v2, 6.1), а сервис на одном аккаунте это означает простой.
    """

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def get_messages(
        self,
        entity: int | str,
        *,
        search: str,
        limit: int,
    ) -> Sequence[MessageLike]: ...


def internal_chat_id(tg_id: int) -> int:
    """Id для ссылки `t.me/c/…` — без служебного префикса `-100`.

    Telethon отдаёт id супергруппы размеченным: `-1001234567890`. Ссылка
    открывается только по внутреннему числу — `1234567890`. Префикс снимается
    исключительно у отрицательных id: положительное число уже внутренняя
    форма, а начинаться со `100` оно вправе само по себе. Из реестра чатов
    положительный id до сюда не доходит — адаптер отвергает такую запись.
    """
    if tg_id >= 0:
        return tg_id
    digits = str(-tg_id)
    if digits.startswith(CHANNEL_ID_PREFIX) and len(digits) > len(CHANNEL_ID_PREFIX):
        return int(digits[len(CHANNEL_ID_PREFIX) :])
    # Обычная группа (не супергруппа): у неё ссылок на сообщения не бывает
    # вовсе. Отдаём число как есть — пусть ссылка будет заведомо неверной и
    # заметной, а не правдоподобной.
    return -tg_id


def topic_id(message: MessageLike) -> int | None:
    """Тема форума, в которой лежит сообщение, или `None`.

    Барахолки часто включают темы, и тогда ссылка без номера темы ведёт в
    никуда. Telegram помечает такое сообщение как ответ: `forum_topic`
    поднят, а сама тема — это `reply_to_top_id`, либо `reply_to_msg_id`, если
    сообщение отвечает прямо на корень темы (тогда `top_id` пуст).
    """
    reply = message.reply_to
    if reply is None or not reply.forum_topic:
        return None
    return reply.reply_to_top_id or reply.reply_to_msg_id


def message_link(chat: ChatLike, msg_id: int, topic: int | None = None) -> str:
    """Ссылка на оригинал поста.

    У публичной группы с username это `t.me/<username>/<id>` — открывается у
    кого угодно, в том числе у клиента, который в группе не состоит. Форма
    `t.me/c/<id>/<msg>` работает только для участников, поэтому она запасная,
    а не основная: карточка со ссылкой, которую клиент не может открыть, —
    это карточка без ссылки. В форуме между чатом и сообщением встаёт третий
    сегмент — номер темы.
    """
    root = f"https://t.me/{chat.username.lstrip('@')}" if chat.username else None
    if root is None:
        root = f"https://t.me/c/{internal_chat_id(chat.tg_id)}"
    if topic is not None:
        return f"{root}/{topic}/{msg_id}"
    return f"{root}/{msg_id}"
