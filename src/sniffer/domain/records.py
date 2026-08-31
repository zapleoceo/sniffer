"""Записи хранилища в терминах домена.

Репозиторий отдаёт наружу эти dataclass-ы, а не ORM-сущности. Причина
практическая: ORM-объект тащит за собой сессию — после закрытия `async with`
любое обращение к незагруженному полю падает `DetachedInstanceError` уже в
боте, далеко от места, где сессия закрылась. Ещё это держит правило слоёв:
`domain` не знает ни про SQLAlchemy, ни про Postgres.

`id: int | None` — одна и та же запись до и после вставки. Отдельный тип
`NewListing` рядом с `Listing` дублировал бы знание о полях ради одного
необязательного поля.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sniffer.domain.passport import Passport

STAGE_PENDING = "pending"


@dataclass(frozen=True, slots=True)
class Chat:
    """Отслеживаемое сообщество. Список курируется вручную."""

    tg_id: int
    title: str
    city: str
    username: str | None = None
    categories: list[str] = field(default_factory=list)
    is_active: bool = True
    search_rank: int = 100
    msg_count_24h: int = 0
    last_msg_id: int = 0
    last_synced_at: datetime | None = None
    added_at: datetime | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class RawMessage:
    """Сырьё как пришло из Telegram. Живёт 30 дней."""

    chat_tg_id: int
    msg_id: int
    text: str
    text_hash: str
    posted_at: datetime
    seller_id: int | None = None
    has_media: bool = False
    stage: str = STAGE_PENDING
    gate_signals: dict[str, Any] = field(default_factory=dict)
    ingested_at: datetime | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class Listing:
    """Нормализованная карточка предложения — то, что ищет клиент."""

    raw_message_id: int
    deal_type: str
    category: str
    city: str
    title: str
    summary: str
    tg_link: str
    posted_at: datetime
    seller_id: int | None = None
    district: str | None = None
    price_amount: Decimal | None = None
    price_currency: str | None = None
    price_period: str | None = None
    price_usd_month: Decimal | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    lang: str | None = None
    confidence: float = 0.0
    extracted_at: datetime | None = None
    is_active: bool = True
    id: int | None = None


@dataclass(frozen=True, slots=True)
class User:
    """Клиент бота."""

    tg_user_id: int
    username: str | None = None
    lang: str = "ru"
    is_blocked: bool = False
    created_at: datetime | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class StoredPassport:
    """Паспорт с версией и владельцем.

    Сам паспорт неизменяем (architecture.md, раздел 5): правка поля создаёт
    новую версию с тем же корнем, старая остаётся для разбора «почему выдача
    изменилась».
    """

    id: int
    user_id: int
    version: int
    passport: Passport
    root_id: int | None = None
    is_current: bool = True
    created_at: datetime | None = None

    @property
    def root(self) -> int:
        """Корень цепочки версий.

        У первой версии `root_id` в базе NULL — корнем ей служит собственный
        id. Иначе подписке (`subscriptions.passport_root`) не на что ссылаться
        до появления второй версии.
        """
        return self.root_id if self.root_id is not None else self.id


@dataclass(frozen=True, slots=True)
class Job:
    """Задача внутренней очереди."""

    id: int
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    run_after: datetime | None = None
