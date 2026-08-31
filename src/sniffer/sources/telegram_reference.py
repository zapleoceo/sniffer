"""Контракт адаптера telegram_groups: константы, протоколы, ссылка на пост.

Отдельно от адаптера по двум причинам. Первая — протоколы здесь нужны трём
сторонам сразу: адаптеру, слою БД (он отдаёт реестр чатов) и тестам. Вторая —
`ChatDirectory` объявлен как протокол намеренно: реестр чатов живёт в таблице
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

# Своя доля от 90 с на весь план (spec-v2, 2.3): у telegram_groups может быть
# несколько задач, и первая же не должна съедать бюджет остальных источников.
SEARCH_BUDGET_S = 40.0

# Пауза после FloodWait растёт: Telegram называет минимум, а повтор подряд
# означает, что минимума мало. Ретрая в цикле нет — попыток на чат две.
FLOOD_BACKOFF_BASE = 2.0
MAX_ATTEMPTS_PER_CHAT = 2

# Префикс, которым Telegram размечает id супергрупп и каналов: -100 + id.
CHANNEL_ID_PREFIX = "100"


@dataclass(slots=True, frozen=True)
class ChatRef:
    """Чат из реестра — ровно то, что адаптеру нужно для запроса и ссылки.

    `tg_id` хранится в размеченной форме, той самой, что отдаёт Telethon в
    `message.chat_id`: у супергруппы это `-100` + внутренний id. Форма важна:
    голое положительное число Telethon примет за id пользователя.

    `search_rank` — порядок обхода, **меньший идёт раньше** (в таблице
    `chats` это колонка со значением по умолчанию 100, то есть новый чат
    становится за курируемыми). Обходим не весь реестр, а первые
    `MAX_CHATS_PER_SEARCH`, поэтому порядок решает, что клиент вообще увидит.
    """

    tg_id: int
    title: str = ""
    username: str = ""
    search_rank: int = 100


class ChatDirectory(Protocol):
    """Реестр чатов: откуда адаптер узнаёт, где искать.

    Реализуется слоем `db` поверх таблицы `chats`; фильтрация по городу,
    `is_active` и порядок по `search_rank` — его работа. Адаптер всё равно
    обрежет список до бюджета сам: реализация, забывшая про `limit`, не
    должна превращаться во FloodWait.
    """

    async def active_chats(self, city: str, limit: int) -> Sequence[ChatRef]: ...


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
    форма, а начинаться со `100` оно вправе само по себе.
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


def message_link(chat: ChatRef, msg_id: int) -> str:
    """Ссылка на оригинал поста.

    У публичной группы с username это `t.me/<username>/<id>` — открывается у
    кого угодно, в том числе у клиента, который в группе не состоит. Форма
    `t.me/c/<id>/<msg>` работает только для участников, поэтому она запасная,
    а не основная: карточка со ссылкой, которую клиент не может открыть, —
    это карточка без ссылки.
    """
    if chat.username:
        return f"https://t.me/{chat.username.lstrip('@')}/{msg_id}"
    return f"https://t.me/c/{internal_chat_id(chat.tg_id)}/{msg_id}"
