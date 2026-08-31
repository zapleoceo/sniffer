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

    # Поведение
    # Пять карточек — лимит бесплатного тарифа (spec-v2, 5.1). Тарифов ещё нет,
    # но число правится конфигом, а не правкой кода: платный тариф отличается
    # от бесплатного значением, а не веткой в рендере.
    max_cards: int = 5
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
