"""Настройки процесса. Единственный источник конфигурации — окружение."""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _empty_to_zero(v: object) -> object:
    """Пустое значение переменной окружения приводим к нулю."""
    if isinstance(v, str) and not v.strip():
        return 0
    return v


def _blank_to(default: int) -> Callable[[object], object]:
    """Пустое значение в `.env` — «не заведено»: берём умолчание, а не роняем процесс."""

    def convert(v: object) -> object:
        return default if isinstance(v, str) and not v.strip() else v

    return convert


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Telegram — бот (клиентский интерфейс)
    bot_token: str = ""
    # `test` — тестовая среда Telegram (отдельные аккаунты и боты, звёзды бесплатны): так
    # проверяют оплату без списаний (docs/payments-live-check.md). В бою пусто или `prod`.
    # Токен тестового бота в боевой среде не работает, и наоборот: перепутать нечем.
    telegram_env: str = "prod"
    # Продажа подписки включена. ВЫКЛ по умолчанию и не выводится из `OWNER_CHAT_ID`: владелец
    # задан в проде всегда, а продавать можно только когда оплаченный слот реально работает
    # (слот мониторинга, возвраты, сверка). Включает владелец явно: `SALES_ENABLED=true`.
    # Уже выданные ссылки при выключенном флаге продолжают приниматься: платёж не теряется.
    sales_enabled: bool = False
    # Сверка платежей: off — не ходит; report — только сообщает владельцу; refund — ещё и
    # записывает недостающее и возвращает «не наши» платежи. По умолчанию report: порядок
    # истории звёзд и поля возвратов живьём не проверены (docs/payments-live-check.md).
    reconcile_mode: Literal["off", "report", "refund"] = "report"
    # Срок ответа на обращения по оплате (`/paysupport`), часов. Пишется в условиях и в
    # ответе клиенту: Telegram требует отвечать на такие обращения вовремя.
    paysupport_reply_hours: Annotated[int, BeforeValidator(_blank_to(48))] = Field(
        default=48, ge=1, le=720
    )

    # Темы Telegram (Threaded mode) в личном чате: поиск = тема (docs/search-tabs.md). Флаг-
    # выключатель: молодой API уже ронял отправку в темы (Bot API 10.0, 08.05.2026). Включается
    # только вместе с `getMe().has_topics_enabled`; режим в @BotFather включает владелец.
    topics_enabled: bool = False

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
    # Закрепление модели по ролям (docs/architecture.md, «Модель по роли»).
    # Имена сверены с каталогом брокера (providers/specs.py, 04.10.2026).
    # Пустая строка в .env = не закреплять: идёт цепочка брокера по capability.
    broker_model_intake: str = "gemini/gemini-3.5-flash-lite"
    broker_model_planner: str = "gemini/gemini-3.5-flash-lite"
    broker_model_offer_screen: str = "gemini/gemini-3.5-flash-lite"
    broker_model_extraction: str = "gemini/gemini-3.5-flash-lite"
    broker_model_guard: str = "gemini/gemini-3.6-flash"

    # Cloudflare R2 — пусто означает «медиа не сохраняем»
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = "sniffer-media"

    # Веб-интерфейс владельца
    # ВНИМАНИЕ: эта настройка НЕ ограничивает доступ. Она говорит, на каком
    # интерфейсе слушает процесс, и по умолчанию это все интерфейсы — иначе
    # проброс порта из контейнера не работает: наружу для процесса выглядит как
    # чужой адрес, и на loopback внутри namespace до него никто не дойдёт.
    # Доступ ограничивает проброс в docker-compose.yml (`127.0.0.1:8005:8005`).
    # Запускаешь дашборд вне compose — задай `DASHBOARD_HOST=127.0.0.1` руками
    # или закрой порт фаерволом: иначе страница с перепиской клиентов слушает
    # все интерфейсы машины в обход TLS и проверки подписи.
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
    # Пять карточек — лимит бесплатного тарифа (spec-v2, 5.1). Тарифов ещё нет,
    # но число правится конфигом, а не правкой кода: платный тариф отличается
    # от бесплатного значением, а не веткой в рендере.
    max_cards: int = 5
    # `listings` — ответ из собственного каталога (`bot/catalog_search.py`):
    # детерминированный SQL по `listings`, без модели и без обхода источников.
    # `catalog` — проверенный каталог агента (`agent_app`), `legacy` — живой
    # поиск по источникам (docs/catalog-dialogue.md).
    catalog_mode: Literal["legacy", "shadow", "pilot", "catalog", "listings"] = "legacy"
    # Internal users.id, NOT Telegram IDs. Empty pilot list enables nobody.
    catalog_pilot_user_ids: tuple[int, ...] = ()
    agent_collector_enabled: bool = False
    agent_collector_interval_s: int = Field(default=3600, ge=3600, le=86400)
    # Периодический обход доски Chotot в общий каталог `listings`
    # (`worker/chotot_sync.py`): доска событий не шлёт, свежее там появляется
    # только по опросу. Не чаще раза в пять минут — мы гости на
    # недокументированном API.
    chotot_sync_interval_s: int = Field(default=1800, ge=300, le=86400)
    live_search_max_chats: int = 10
    live_search_cache_ttl_s: int = 300
    # Архив нужен, чтобы догонять объявления, пришедшие до вступления в чат.
    # 30 дней противоречили схеме и документации, где срок — 90 дней.
    raw_retention_days: int = 90
    # Сколько подписок монитор берёт за один проход (`worker/monitor.py`). Это размер
    # порции, а не потолок числа подписок: остальные дойдут на следующих проходах — обход
    # идёт по кругу, «кого не смотрели дольше всех — первым». Прежний `LIMIT 50` без
    # ротации навсегда прятал 51-ю подписку (D4 разведки R5).
    monitor_batch: int = Field(default=50, ge=1, le=500)
    # Сколько часов после окончания оплаченного срока ещё доходит УЖЕ найденное в
    # оплаченный период (D7). Клиент заплатил за находки, а не за момент, когда нотифаер
    # успел их отправить: сообщение, стоявшее в очереди на момент окончания, доживает
    # эту льготу, дальше его отменяют (`outbox.status = 'cancelled'`), а не шлют
    # вчерашнее неплательщику. Новое после окончания не ставится вовсе — без льготы.
    monitor_lapse_grace_hours: int = Field(default=6, ge=0, le=168)
    prefilter_batch: int = 20
    extract_batch: int = 10
    # Срок годности строки очереди доставки (notifier), в часах от времени, на
    # которое она назначена. После простоя нотифаера «мгновенное» уведомление
    # двухдневной давности — это не новость, а шум: оно отменяется, а не уходит.
    # 24 часа для всех. Шесть часов после окончания подписки — другое правило, его
    # применяет матчер (`monitor_lapse_grace_hours`). Значение повторено в
    # `notifier.policy.Policy`, их равенство сторожит тест.
    outbox_ttl_h: int = Field(default=24, ge=1)
    default_city: str = "nha_trang"
    log_level: str = "INFO"

    @property
    def selling(self) -> bool:
        """Подписку предлагают людям: флаг включён И есть кому отвечать за возвраты.

        Единственное место, где это условие записано для слов и кнопок бота: пока оно ложно,
        экраны не зовут купить и не рисуют «Подписку» как покупку (`bot/sales_gate.py`).
        """
        return self.sales_enabled and self.owner_chat_id != 0

    @property
    def telegram_test_environment(self) -> bool:
        return self.telegram_env.strip().lower() == "test"

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
