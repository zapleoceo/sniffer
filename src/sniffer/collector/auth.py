"""Разовая интерактивная авторизация юзербота: выдаёт `StringSession`.

Нужна ровно один раз на аккаунт. Telethon умеет хранить сессию файлом, но
файл не переживает пересоздание контейнера, поэтому владелец получает строку и
кладёт её в `.env` как `TG_SESSION` — дальше коллектор стартует без вопросов.

**Строка сессии — это полный доступ к аккаунту, не хуже пароля.** Поэтому она
здесь нигде не логируется: ни через structlog, ни через `logging`, ни в тексте
исключения. Единственный её выход наружу — одна строка в stdout. Подсказки и
приглашения ввода идут в stderr, так что `... auth 2>/dev/null` даёт ровно
строку сессии и ничего больше.

Юзербот только читает (CLAUDE.md, spec-v2 6.1). Из телеграм-методов здесь
вызываются только те, без которых авторизации не существует: `connect`,
`send_code_request`, `sign_in`, `disconnect`. Их список зафиксирован в
протоколе `TelegramLike` — добавить сюда отправку сообщения молча не выйдет.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass
from getpass import getpass
from typing import Protocol

from telethon.errors import RPCError, SessionPasswordNeededError

from sniffer.config import Settings, get_settings

EXIT_OK = 0
EXIT_NOT_CONFIGURED = 2
EXIT_TELEGRAM_REFUSED = 3

# Код подтверждения Telegram присылает СООБЩЕНИЕМ В САМ TELEGRAM, а не в SMS.
# Без этой подсказки владелец полминуты смотрит в телефон и ждёт эсэмэску,
# которой не будет.
CODE_PROMPT = "Код подтверждения (придёт сообщением в Telegram, не в SMS): "
PASSWORD_PROMPT = "Пароль двухфакторной защиты (ввод не отображается): "


class SessionLike(Protocol):
    """Хранилище сессии Telethon в той части, которой мы пользуемся."""

    def save(self) -> str: ...


class TelegramLike(Protocol):
    """Ровно те методы Telethon, которые нужны для входа. Больше — нельзя."""

    # Свойство, а не поле: изменяемый атрибут в протоколе инвариантен, и
    # тогда ни один клиент со своим типом сессии в него не укладывается.
    @property
    def session(self) -> SessionLike: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def send_code_request(self, phone: str) -> object: ...

    async def sign_in(
        self,
        phone: str | None = ...,
        code: str | None = ...,
        *,
        password: str | None = ...,
    ) -> object: ...


ClientFactory = Callable[[int, str], TelegramLike]


def _new_client(api_id: int, api_hash: str) -> TelegramLike:
    """Импорт внутри функции: тестам Telethon-клиент не нужен вовсе."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client: TelegramLike = TelegramClient(StringSession(), api_id, api_hash)
    return client


def _say(text: str) -> None:
    print(text, file=sys.stderr)


def _ask(prompt: str) -> str:
    # Приглашение — в stderr, иначе оно смешается со строкой сессии в stdout.
    print(prompt, file=sys.stderr, end="", flush=True)
    return input().strip()


@dataclass(slots=True, frozen=True)
class Console:
    """Ввод-вывод команды одним объектом — чтобы тест не трогал терминал."""

    say: Callable[[str], None] = _say
    ask: Callable[[str], str] = _ask
    # getpass сам печатает приглашение в stderr и гасит эхо: пароль
    # двухфакторной защиты не должен оставаться в истории терминала.
    ask_secret: Callable[[str], str] = getpass


def missing_auth_settings(settings: Settings) -> list[str]:
    """Имена пустых настроек так, как они названы в `.env`.

    `TG_SESSION` здесь не нужен — его эта команда и создаёт.
    """
    required = {
        "TG_API_ID": bool(settings.tg_api_id),
        "TG_API_HASH": bool(settings.tg_api_hash.strip()),
        "TG_PHONE": bool(settings.tg_phone.strip()),
    }
    return [name for name, filled in required.items() if not filled]


async def authorize(
    settings: Settings,
    console: Console,
    *,
    client_factory: ClientFactory = _new_client,
) -> str:
    """Проводит вход и возвращает строку сессии. Не печатает и не логирует её."""
    phone = settings.tg_phone.strip()
    client = client_factory(settings.tg_api_id, settings.tg_api_hash.strip())
    await client.connect()
    try:
        console.say(f"Запрашиваю код для {phone}…")
        await client.send_code_request(phone)
        try:
            await client.sign_in(phone, console.ask(CODE_PROMPT))
        except SessionPasswordNeededError:
            # Аккаунт с включённой двухфакторной защитой: код принят, но
            # Telegram ждёт ещё и облачный пароль.
            await client.sign_in(password=console.ask_secret(PASSWORD_PROMPT))
        return client.session.save()
    finally:
        await client.disconnect()


def run_auth(
    settings: Settings | None = None,
    console: Console | None = None,
    *,
    client_factory: ClientFactory = _new_client,
) -> int:
    """Команда целиком: проверка настроек, вход, печать строки. Код возврата."""
    settings = settings or get_settings()
    console = console or Console()

    missing = missing_auth_settings(settings)
    if missing:
        console.say(
            f"Не хватает настроек: {', '.join(missing)}. "
            "Заполните их в .env и повторите — авторизоваться без них негде."
        )
        return EXIT_NOT_CONFIGURED

    try:
        session = asyncio.run(authorize(settings, console, client_factory=client_factory))
    except RPCError as err:
        # Печатаем класс и текст ошибки Telegram, но не трейсбек: владельцу
        # нужно знать «код неверный» или «подождите N секунд», а не стек.
        console.say(f"Telegram отказал: {type(err).__name__}: {err}")
        return EXIT_TELEGRAM_REFUSED

    console.say("Строка сессии ниже. Впишите её в .env как TG_SESSION и никому не показывайте:")
    # Единственное место, где сессия покидает процесс. В лог — никогда.
    print(session)
    return EXIT_OK
