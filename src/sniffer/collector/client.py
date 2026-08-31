"""Наш MTProto-клиент: как представляемся и чем пользуемся.

Модуль отвечает на два вопроса, и оба важны отдельно от сценария входа.

**Как аккаунт выглядит в Telegram → Настройки → Устройства.** Без явных
`device_model` / `system_version` / `app_version` там висит безымянный клиент,
и владелец не отличит наш сеанс от чужого, когда решает, какой отозвать.

**Чем нам можно пользоваться.** `TelegramLike` — граница для проверки типов, а
не песочница: настоящий объект остаётся полноценным `TelegramClient` и в
рантайме умеет всё. Но попытка дописать в код отправку сообщения ломает mypy, и
«юзербот только читает» (CLAUDE.md, spec-v2 6.1) перестаёт быть обещанием в
комментарии. Рантайм-гарантию даёт ревью, а не этот класс.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

DEVICE_MODEL = "SnifferBot collector"
SYSTEM_VERSION = "docker"
# Значения обязан повторить и сам коллектор, когда получит тело: другой
# device_model — другая строка в списке устройств, как будто вошли заново.
APP_VERSION = "0.1"


class SessionLike(Protocol):
    """Хранилище сессии Telethon в той части, которой мы пользуемся."""

    def save(self) -> str: ...


class TelegramLike(Protocol):
    """Ровно те методы, без которых авторизации не существует."""

    # Свойство, а не поле: изменяемый атрибут в протоколе инвариантен, и
    # тогда ни один клиент со своим типом сессии в него не укладывается.
    @property
    def session(self) -> SessionLike: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def send_code_request(self, phone: str) -> object: ...

    # Единственный надёжный ответ на вопрос «вход состоялся?». Возврат
    # `sign_in` им не является: у Telethon есть ветка, где он отдаёт объект
    # отправленного кода вместо пользователя, и она не бросает исключения.
    async def is_user_authorized(self) -> bool: ...

    async def sign_in(
        self,
        phone: str | None = ...,
        code: str | None = ...,
        *,
        password: str | None = ...,
    ) -> object: ...


ClientFactory = Callable[[int, str], TelegramLike]


def new_client(api_id: int, api_hash: str) -> TelegramLike:
    """Импорт внутри функции: тестам Telethon-клиент не нужен вовсе."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client: TelegramLike = TelegramClient(
        StringSession(),
        api_id,
        api_hash,
        device_model=DEVICE_MODEL,
        system_version=SYSTEM_VERSION,
        app_version=APP_VERSION,
    )
    return client
