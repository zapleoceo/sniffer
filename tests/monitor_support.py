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
class FakeDelivery:
    """Очередь доставки без базы: записывает, что и с каким временем ей поставили."""

    queued: list[dict[str, Any]] = field(default_factory=list)
    claims: list[dict[str, Any]] = field(default_factory=list)
    advanced: list[tuple[int, int]] = field(default_factory=list)
    subscriptions: list[SubscriptionState] = field(default_factory=list)

    async def active_subscriptions(
        self, *, limit: int = 200, now: datetime | None = None
    ) -> list[SubscriptionState]:
        self.claims.append({"limit": limit, "now": now})
        return list(self.subscriptions)

    async def used_since(self, subscription_id: int, *, since: datetime) -> int:
        return 0

    async def enqueue(self, **kwargs: Any) -> bool:
        self.queued.append(kwargs)
        return True

    async def advance_scan(self, subscription_id: int, listing_id: int) -> None:
        self.advanced.append((subscription_id, listing_id))


@dataclass
class FakeListings:
    """Каталог без базы: отдаёт заранее заданную страницу и запоминает вопросы к ней."""

    page: list[Listing] = field(default_factory=list)
    asked: list[tuple[MatchFilter, int, int]] = field(default_factory=list)

    async def match(
        self, spec: MatchFilter, *, after_id: int = 0, limit: int = 50
    ) -> list[Listing]:
        self.asked.append((spec, after_id, limit))
        return list(self.page)


@dataclass
class World:
    session: FakeSession
    delivery: FakeDelivery
    listings: FakeListings


def install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    subscriptions: list[SubscriptionState] | None = None,
    page: list[Listing] | None = None,
) -> World:
    """Подменить сессию и репозитории матчера; вернуть то, на что можно смотреть."""
    from sniffer.worker import matcher as module

    world = World(
        session=FakeSession(),
        delivery=FakeDelivery(subscriptions=list(subscriptions or [])),
        listings=FakeListings(page=list(page or [])),
    )
    monkeypatch.setattr(module, "session_scope", lambda: Scope(world.session))
    monkeypatch.setattr(module, "DeliveryRepository", lambda _session: world.delivery)
    monkeypatch.setattr(module, "ListingRepository", lambda _session: world.listings)
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
