"""Заготовки тестов монитора: подписка, карточка и подмена репозиториев без базы.

Отдельным файлом, а не копией в каждом тесте: проход матчера проверяют сразу несколько
файлов (часы, курс, изоляция подписок), и две копии подделки репозитория разошлись бы на
первой правке интерфейса. Подделка здесь проверяет ОРКЕСТРАЦИЮ прохода — кого вызвали, с
каким временем, что откатили. Что делает сам SQL, проверяет живая база
(`test_db_monitor.py`) и разбор текста запросов (`test_monitor_sql.py`): подделка
репозитория проверяла бы подделку.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any

import pytest

from sniffer.db.repositories.monitors import BrokenSubscription, DueSubscriptions
from sniffer.domain.monitoring import Overflow
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.domain.records import Listing, MatchFilter, StoredPassport, SubscriptionState

# Время в тестах без базы зашито намеренно: проход получает его аргументом, и значение не
# должно зависеть от часов машины. Модули с живой базой так не делают — см.
# `test_db_clock_rule.py`.
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def passport(**overrides: object) -> Passport:
    fields: dict[str, object] = {
        "intent": Intent.BUY,
        "category": Category.MOTORBIKE,
        "city": "nha_trang",
    }
    fields.update(overrides)
    return Passport(**fields)  # type: ignore[arg-type]


def dollars(amount: float) -> Budget:
    return Budget(max=amount, currency=Currency.USD)


def subscription(number: int = 1, **overrides: object) -> SubscriptionState:
    fields: dict[str, object] = {
        "id": number,
        "user_id": 100 + number,
        "passport_root": 200 + number,
        "max_per_day": 5,
        "passport": StoredPassport(
            id=200 + number, user_id=100 + number, version=1, passport=passport()
        ),
    }
    fields.update(overrides)
    return SubscriptionState(**fields)  # type: ignore[arg-type]


def usd_subscription(number: int = 1, amount: float = 300) -> SubscriptionState:
    """Подписка с долларовым бюджетом: потолок в донгах ей даёт только курс."""
    stored = StoredPassport(
        id=200 + number,
        user_id=100 + number,
        version=1,
        passport=passport(budget=dollars(amount)),
    )
    return subscription(number, passport=stored)


def subscription_in(number: int, city: str | None, **overrides: object) -> SubscriptionState:
    """Подписка на запрос в заданном городе: по городу подменный каталог узнаёт «больную»."""
    stored = StoredPassport(
        id=200 + number, user_id=100 + number, version=1, passport=passport(city=city)
    )
    return subscription(number, passport=stored, **overrides)


def listing(number: int = 1, *, moment: datetime = NOW, **overrides: object) -> Listing:
    fields: dict[str, object] = {
        "id": number,
        "raw_message_id": number,
        "deal_type": "sell",
        "category": "motorbike",
        "city": "nha_trang",
        "title": f"Honda Vision {number}",
        "summary": "Автомат",
        "tg_link": f"https://t.me/c/1/{number}",
        # Час назад: score свежей карточки выше порога при любом «сейчас» теста.
        "posted_at": moment - timedelta(hours=1),
    }
    fields.update(overrides)
    return Listing(**fields)  # type: ignore[arg-type]


class Nested:
    """Подделка SAVEPOINT: помнит, откатили ли его, и не глотает исключение."""

    def __init__(self, session: FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> Nested:
        self._session.savepoints += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if exc_type is not None:
            self._session.rolled_back += 1
        return False


class FakeSession:
    def __init__(self) -> None:
        self.savepoints = 0
        self.rolled_back = 0
        self.commits = 0

    def begin_nested(self) -> Nested:
        return Nested(self)

    async def commit(self) -> None:
        self.commits += 1


class Scope:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> FakeSession:
        return self._session

    async def __aexit__(self, *_: object) -> None:
        return None


@dataclass
class FakeMonitors:
    """Монитор подписок без базы: отдаёт заданную порцию и записывает отметки обхода."""

    ready: list[SubscriptionState] = field(default_factory=list)
    broken: list[BrokenSubscription] = field(default_factory=list)
    claims: list[dict[str, Any]] = field(default_factory=list)
    claim_error: BaseException | None = None
    cancellations: list[dict[str, Any]] = field(default_factory=list)
    lapsed: int = 0
    order: list[str] = field(default_factory=list)
    touched: list[int] = field(default_factory=list)
    scanned: list[int] = field(default_factory=list)
    quarantined: list[dict[str, Any]] = field(default_factory=list)
    # Слоты клиентов в порядке приоритета; не заданы — все подписки порции в порядке id.
    ranks: dict[int, list[int]] | None = None
    no_slot: dict[int, datetime | None] = field(default_factory=dict)
    overflow: list[tuple[int, Overflow, int]] = field(default_factory=list)

    async def ranked_slots(self, user_ids: Any, *, now: datetime) -> dict[int, list[int]]:
        if self.ranks is not None:
            return {user: list(self.ranks.get(user, [])) for user in user_ids}
        ranked: dict[int, list[int]] = {}
        for subscription in sorted(self.ready, key=lambda item: item.id):
            ranked.setdefault(subscription.user_id, []).append(subscription.id)
        return ranked

    async def set_no_slot_since(self, subscription_id: int, since: datetime | None) -> None:
        self.no_slot[subscription_id] = since

    async def record_overflow(
        self, subscription_id: int, overflow: Overflow, *, extra: int
    ) -> None:
        self.overflow.append((subscription_id, overflow, extra))

    async def cancel_lapsed(self, *, now: datetime, grace: timedelta) -> int:
        self.order.append("cancel")
        self.cancellations.append({"now": now, "grace": grace})
        return self.lapsed

    async def claim_due(self, *, limit: int, now: datetime) -> DueSubscriptions:
        self.order.append("claim")
        self.claims.append({"limit": limit, "now": now})
        if self.claim_error is not None:
            raise self.claim_error
        return DueSubscriptions(ready=list(self.ready), broken=list(self.broken))

    async def touch(self, subscription_id: int, *, now: datetime) -> None:
        self.touched.append(subscription_id)

    async def record_scan(self, subscription_id: int, *, now: datetime) -> None:
        self.scanned.append(subscription_id)

    async def quarantine(
        self, subscription_id: int, *, now: datetime, streak: int, until: datetime, error: str
    ) -> None:
        self.quarantined.append(
            {"id": subscription_id, "now": now, "streak": streak, "until": until, "error": error}
        )


@dataclass
class FakeDelivery:
    """Очередь доставки без базы: записывает, что и с каким временем ей поставили."""

    queued: list[dict[str, Any]] = field(default_factory=list)
    advanced: list[tuple[int, int]] = field(default_factory=list)
    used: int = 0
    used_since_args: list[datetime] = field(default_factory=list)
    notices: list[dict[str, Any]] = field(default_factory=list)
    bumped: list[int] = field(default_factory=list)

    async def enqueue_notice(self, **kwargs: Any) -> None:
        self.notices.append(kwargs)

    async def bump_overflow_notice(self, subscription_id: int, *, count: int) -> int:
        self.bumped.append(count)
        return 1

    async def used_since(self, subscription_id: int, *, since: datetime) -> int:
        self.used_since_args.append(since)
        return self.used

    async def enqueue(self, **kwargs: Any) -> bool:
        self.queued.append(kwargs)
        return True

    async def advance_scan(self, subscription_id: int, listing_id: int) -> None:
        self.advanced.append((subscription_id, listing_id))


@dataclass
class FakeListings:
    """Каталог без базы: отдаёт заданную страницу и запоминает вопросы к ней.

    `explode` — «больная» карточка или паспорт: вопрос по такому городу падает заданным
    исключением, как падает настоящий запрос на неразобранных данных.
    """

    page: list[Listing] = field(default_factory=list)
    asked: list[tuple[MatchFilter, int, int]] = field(default_factory=list)
    explode: dict[str, BaseException] = field(default_factory=dict)
    # Верхняя граница id в базе (водяной знак) и первая карточка без вердикта ИИ-проверки.
    head: int = 10**9
    unready: int | None = None
    before_ids: list[int | None] = field(default_factory=list)
    verdict_cutoffs: list[datetime] = field(default_factory=list)

    async def max_id(self) -> int:
        return self.head

    async def first_unready_id(self, *, after_id: int, verdict_before: datetime) -> int | None:
        self.verdict_cutoffs.append(verdict_before)
        return self.unready

    async def match(
        self,
        spec: MatchFilter,
        *,
        after_id: int = 0,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[Listing]:
        self.asked.append((spec, after_id, limit))
        self.before_ids.append(before_id)
        if spec.city in self.explode:
            raise self.explode[spec.city]
        return [
            item
            for item in self.page
            if (item.id or 0) > after_id and (before_id is None or (item.id or 0) < before_id)
        ]


@dataclass
class FakeLedger:
    """Журнал показов без базы: что уже видел клиент и что слежение записало."""

    already: set[int] = field(default_factory=set)
    claims: list[Any] = field(default_factory=list)
    # Период клиента начат другим показом? Нет — слежение резерва не пишет, якорь поставит доставка.
    started: bool = True

    async def seen(self, user_id: int, listing_ids: Any, now: datetime) -> set[int]:
        return {i for i in listing_ids if i in self.already}

    async def reserve(self, claim: Any) -> None:
        self.claims.append(claim)

    async def reserve_if_started(self, claim: Any) -> bool:
        if not self.started:
            return False
        await self.reserve(claim)
        return True


@dataclass
class World:
    session: FakeSession
    monitors: FakeMonitors
    delivery: FakeDelivery
    listings: FakeListings
    ledger: FakeLedger


def install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    subscriptions: list[SubscriptionState] | None = None,
    broken: list[BrokenSubscription] | None = None,
    page: list[Listing] | None = None,
    explode: dict[str, BaseException] | None = None,
) -> World:
    """Подменить сессию и репозитории матчера; вернуть то, на что можно смотреть."""
    from sniffer.worker import monitor as module

    world = World(
        session=FakeSession(),
        monitors=FakeMonitors(ready=list(subscriptions or []), broken=list(broken or [])),
        delivery=FakeDelivery(),
        listings=FakeListings(page=list(page or []), explode=dict(explode or {})),
        ledger=FakeLedger(),
    )
    monkeypatch.setattr(module, "session_scope", lambda: Scope(world.session))
    monkeypatch.setattr(module, "MonitorRepository", lambda _session: world.monitors)
    monkeypatch.setattr(module, "DeliveryRepository", lambda _session: world.delivery)
    monkeypatch.setattr(module, "ListingRepository", lambda _session: world.listings)
    monkeypatch.setattr(module, "QuotaRepository", lambda _session: world.ledger)
    return world


class _Result:
    """Ответ базы, из которого репозитории берут только скаляры и строки."""

    def __init__(self, scalar: Any, rows: list[Any]) -> None:
        self._scalar = scalar
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def scalar_one(self) -> Any:
        return self._scalar

    def all(self) -> list[Any]:
        return list(self._rows)

    def __iter__(self) -> Any:
        return iter(self._rows)


class Recorder:
    """Сессия, которая не ходит в базу, а запоминает запросы и добавленные объекты.

    Нужна для одного вопроса: ЧТО ушло в запрос. Какое время, какой потолок, чья строка —
    это видно в параметрах оператора и не требует Postgres. Что запрос ДЕЛАЕТ, проверяет
    только живая база.
    """

    def __init__(self, *, scalar: Any = None, rows: list[Any] | None = None) -> None:
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self._scalar = scalar
        self._rows = rows or []

    async def execute(self, statement: Any, *_: object, **__: object) -> _Result:
        self.statements.append(statement)
        return _Result(self._scalar, self._rows)

    async def scalar(self, statement: Any, *_: object, **__: object) -> Any:
        self.statements.append(statement)
        return self._scalar

    def add(self, item: Any) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        return None


def sql_of(statement: Any) -> str:
    """Текст оператора для Postgres — так, как его увидит база."""
    from sqlalchemy.dialects import postgresql

    return str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]


def params_of(statement: Any) -> dict[str, Any]:
    """Значения, которые уйдут в оператор параметрами."""
    from sqlalchemy.dialects import postgresql

    return dict(statement.compile(dialect=postgresql.dialect()).params)  # type: ignore[no-untyped-call]
