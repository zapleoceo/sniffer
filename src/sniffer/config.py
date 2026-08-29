"""Настройки процесса. Единственный источник конфигурации — окружение."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Telegram — бот (клиентский интерфейс)
    bot_token: str = ""

    # Telegram — юзербот (чтение сообществ)
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_phone: str = ""
    tg_session: str = ""

    database_url: str = "postgresql+asyncpg://sniffer:sniffer@localhost:5433/sniffer"

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
