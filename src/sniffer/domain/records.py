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
from datetime import datetime, time
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
    # Докуда дочитан архив вниз; 0 — добор не начинался. Отдельно от
    # `last_msg_id`, потому что это два курсора в разные стороны по одной
    # ленте, а не одно значение в разных состояниях.
    backfill_msg_id: int = 0
    backfill_done: bool = False
    last_synced_at: datetime | None = None
    added_at: datetime | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    """Кандидат из постоянной очереди вступлений.

    Это не ORM-строка: коллектор получает простую неизменяемую запись и не
    может случайно унести сессию SQLAlchemy к Telegram-вызову, который может
    ждать сеть минутами.
    """

    key: str
    username: str = ""
    invite_hash: str = ""
    found_in: str = ""
    priority: int = 100


@dataclass(frozen=True, slots=True)
class JoinLimits:
    """Снимок лимитов вступления из БД, без знания о Telegram-слое."""

    joins_in_window: int
    next_allowed_at: datetime | None = None
    blocked_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class RawMessage:
    """Сырьё как пришло из Telegram. Непреобразованное живёт 90 дней."""

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

    raw_message_id: int | None
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
    source: str = "telegram_archive"
    external_id: str | None = None
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
class PassportEvent:
    """Что произошло с паспортом: вопрос агента, ответ клиента, обратная связь.

    Заодно это и состояние диалога: сколько вопросов уже задано и ждём ли мы
    ответ, выводится из этого лога, а не хранится отдельно. Отдельная колонка
    рано или поздно разошлась бы с историей, а история нужна и без диалога —
    объяснить клиенту, почему выдача изменилась.
    """

    passport_id: int
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class Job:
    """Задача внутренней очереди."""

    id: int
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    run_after: datetime | None = None


REQUEST_RUNNING = "running"
REQUEST_DONE = "done"
REQUEST_FAILED = "failed"

DIRECTION_IN = "in"
DIRECTION_OUT = "out"


@dataclass(frozen=True, slots=True)
class ClientRequest:
    """Запрос клиента как единица наблюдения.

    К нему привязываются и расходы, и замеры времени: без общего ключа связать
    «спросил про байк» и «потрачено N токенов» можно только по времени, а при
    двух параллельных запросах время врёт.
    """

    id: int
    user_id: int
    raw_query: str
    status: str = REQUEST_RUNNING
    passport_id: int | None = None
    stages: dict[str, int] = field(default_factory=dict)
    plan_fallback: bool = False
    sources: list[str] = field(default_factory=list)
    result_count: int = 0
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None


@dataclass(frozen=True, slots=True)
class DialogMessage:
    """Реплика переписки: `in` — от клиента, `out` — от бота."""

    id: int
    user_id: int
    direction: str
    text: str
    request_id: int | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BrokerCall:
    """Один вызов LLM: что просили, кто ответил, сколько стоило.

    `broker_request_id` — идентификатор строки в `usage_log` брокера. Его
    отсутствие означает, что брокер до учёта не дошёл, а не что вызов был
    бесплатным.
    """

    capability: str
    request_id: int | None = None
    broker_request_id: int | None = None
    provider: str | None = None
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal(0)
    latency_ms: int | None = None
    created_at: datetime | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class SessionState:
    """Состояние сессии юзербота — без самой строки сессии.

    Строку наружу не отдаём принципиально: она нужна только коллектору, а
    дашборд обязан уметь показать состояние, ничего не расшифровывая.
    """

    phone: str
    is_active: bool
    id: int | None = None
    last_ok_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CandidateState:
    """Кандидат очереди вместе с ходом разбора — то, чего нет в `DiscoveryCandidate`.

    Второй тип рядом с первым, а не поля с умолчаниями в нём: `join_next()`
    берёт кандидата, чтобы вступить, и `status` со `attempts` ему только мешают
    — он их не читает и менять не вправе. Показывать очередь человеку без них,
    наоборот, бессмысленно: «застряло» видно ровно по числу попыток.
    """

    key: str
    username: str = ""
    invite_hash: str = ""
    found_in: str = ""
    priority: int = 100
    status: str = "queued"
    attempts: int = 0
    found_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RejectedCandidate:
    """Отклонённый кандидат: ключ, причина, когда."""

    key: str
    reason: str
    rejected_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class JoinEvent:
    """Строка журнала вступлений — как её видит человек, а не страж лимитов.

    Стражу хватает `JoinLimits` (сколько за окно, когда можно снова). Здесь
    нужно другое: чем кончилось конкретное вступление и заглушен ли чат —
    незаглушенный рабочий аккаунт владелец замечает уведомлениями, а не логом.
    """

    kind: str
    happened_at: datetime
    tg_id: int | None = None
    username: str | None = None
    next_allowed_at: datetime | None = None
    blocked_until: datetime | None = None
    muted: bool = False
    mute_error: str | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class SubscriptionState:
    """Подписка вместе с ТЕКУЩЕЙ версией паспорта.

    Паспорт здесь не копия на момент создания, а актуальная версия цепочки:
    клиент правит запрос, и подписка обязана следовать за правкой, а не
    застывать. Поэтому в таблице лежит корень цепочки, а сюда репозиторий
    кладёт то, что этот корень означает сегодня.
    """

    id: int
    user_id: int
    passport_root: int
    passport: StoredPassport
    mode: str = "instant"
    max_per_day: int = 5
    quiet_from: time | None = None
    quiet_to: time | None = None
    # С какой карточки начинается слежение. Подписка обещает НОВЫЕ посты, а не
    # пересказ той выдачи, из которой клиент ничего не выбрал.
    since_listing_id: int = 0
    # Докуда матчер уже ПРОСМОТРЕЛ ленту. В отличие от notifications движется
    # и по неподходящим карточкам, иначе первая страница вечна.
    scan_listing_id: int = 0
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """Сообщение, которое ещё не ушло клиенту."""

    id: int
    user_id: int
    payload: dict[str, Any]
    attempts: int = 0
    scheduled_at: datetime | None = None
    subscription_id: int | None = None
    notification_id: int | None = None


@dataclass(frozen=True, slots=True)
class MatchFilter:
    """Условия отбора карточек — значения, а не запрос.

    Живёт в `domain`, потому что нужна двоим по разные стороны границы:
    `matching` её собирает по паспорту, `db` по ней строит SQL. Положи её в
    `matching` — и репозиторий начнёт импортировать слой, который сам его
    вызывает, то есть появится обратная зависимость, запрещённая CLAUDE.md.
    """

    city: str
    category: str | None = None
    deal_type: str | None = None
    max_price_vnd: Decimal | None = None
    since: datetime | None = None


@dataclass(frozen=True, slots=True)
class Payment:
    """Платёж за подписку.

    `external_id` — `telegram_payment_charge_id`, и он уникален в таблице.
    Telegram повторяет апдейт, если бот не ответил вовремя; без уникальности
    один платёж продлил бы подписку дважды. Деньги нельзя обработать
    «примерно один раз».
    """

    user_id: int
    amount: int
    external_id: str
    subscription_id: int | None = None
    provider: str = "telegram_stars"
    currency: str = "XTR"
    status: str = "paid"
    is_recurring: bool = False
    created_at: datetime | None = None
    id: int | None = None
