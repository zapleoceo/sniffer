"""Настройки процесса. Единственный источник конфигурации — окружение."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import BeforeValidator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _empty_to_zero(v: object) -> object:
    """Пустое значение переменной окружения приводим к нулю."""
    if isinstance(v, str) and not v.strip():
        return 0
    return v


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Telegram — бот (клиентский интерфейс)
    bot_token: str = ""

    # Telegram — юзербот (чтение сообществ)
    # Пустая строка в .env — это "не заведено", а не ошибка типа. Без
    # приведения pydantic валится на TG_API_ID= с int_parsing и роняет процесс
    # ещё до того, как runtime успеет сказать "жду конфигурации".
    tg_api_id: Annotated[int, BeforeValidator(_empty_to_zero)] = 0
    tg_api_hash: str = ""
    tg_phone: str = ""
    tg_session: str = ""

    database_url: str = "postgresql+asyncpg://sniffer:sniffer@localhost:5434/sniffer"

    # AIbroker
    broker_url: str = "https://aib.zapleo.com"
    broker_project_key: str = ""
    broker_timeout_s: int = 120

    # Cloudflare R2 — пусто означает «медиа не сохраняем»
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = "sniffer-media"

    # Веб-интерфейс владельца
    # Порт слушаем только на loopback: снаружи ходит nginx, а не браузер.
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8005
    # Кому можно внутрь. Ноль означает «владелец не задан» — вход закрыт всем.
    owner_chat_id: Annotated[int, BeforeValidator(_empty_to_zero)] = 0
    # Имя бота для Telegram Login Widget. Виджет подписывает данные ключом,
    # производным от BOT_TOKEN, поэтому имя должно принадлежать тому же боту.
    bot_username: str = "RecVNbot"
    # Подписывает НАШУ cookie. Отдельный секрет от BOT_TOKEN намеренно: один
    # ключ на две цели означает, что утечка одной обесценивает обе.
    dashboard_session_secret: str = ""
    # Шифрует строку сессии юзербота в БД. Третий секрет, не переиспользуем
    # предыдущие: у шифрования данных в покое другой срок жизни и другой
    # радиус ущерба, чем у подписи cookie.
    secret_encryption_key: str = ""

    # Поведение
    live_search_max_chats: int = 10
    live_search_cache_ttl_s: int = 300
    raw_retention_days: int = 30
    prefilter_batch: int = 20
    extract_batch: int = 10
    default_city: str = "nha_trang"
    log_level: str = "INFO"

    @property
    def media_enabled(self) -> bool:
        return bool(self.r2_account_id and self.r2_access_key_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """Перечитать окружение заново.

    Нужно процессу, который стартовал без обязательного секрета и ждёт, пока
    его заведут: без сброса кэша он до конца жизни контейнера видел бы пустой
    токен и ждал бы вечно, даже когда `.env` уже поправлен.
    """
    get_settings.cache_clear()
    return get_settings()
