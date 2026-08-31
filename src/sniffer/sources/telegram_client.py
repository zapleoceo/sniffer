"""Откуда адаптер `telegram_groups` берёт свои зависимости.

Отделено от самого поиска намеренно: «как создать клиента и что делать, если
настроек нет» — это не то же самое, что «как искать по чатам», и меняются эти
две вещи по разным поводам.
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from sniffer.config import Settings
from sniffer.sources.telegram_reference import ChatRef, TelegramReader

log = structlog.get_logger(__name__)


class EmptyChatDirectory:
    """Заглушка на время, пока слой `db` не написан: чатов нет.

    Пустой реестр — это «искать негде», а не поломка, поэтому источник молча
    отдаёт пустой список и остаётся в плане. Подменяется одной строкой при
    сборке адаптера, когда появится репозиторий чатов.
    """

    async def active_chats(self, city: str, limit: int) -> Sequence[ChatRef]:
        log.warning("telegram.no_chat_directory", city=city)
        return []


def new_reader(settings: Settings) -> TelegramReader:
    """Импорт внутри функции: тестам Telethon-клиент не нужен вовсе.

    `flood_sleep_threshold=0` — не украшение. По умолчанию Telethon сам спит
    на FloodWait до 60 секунд внутри вызова и повторяет запрос, не сказав ни
    слова наружу. С таким клиентом ни растущая пауза, ни бюджет, ни `degraded`
    из адаптера никогда не сработают: минуту съедят молча, а план ждёт 90 с.
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client: TelegramReader = TelegramClient(
        StringSession(settings.tg_session),
        settings.tg_api_id,
        settings.tg_api_hash,
        flood_sleep_threshold=0,
    )
    return client


def missing_reader_settings(settings: Settings) -> list[str]:
    """Имена пустых настроек так, как они названы в `.env`."""
    required = {
        "TG_API_ID": bool(settings.tg_api_id),
        "TG_API_HASH": bool(settings.tg_api_hash.strip()),
        "TG_SESSION": bool(settings.tg_session.strip()),
    }
    return [name for name, filled in required.items() if not filled]
