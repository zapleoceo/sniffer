"""Связка поисков с хранилищем — без базы: что `PassportStore` и репозиторий делают на самом деле.

Тесты с Postgres (`test_store_contract.py`, `test_thread_repository.py`) в CI идут
всегда, а у разработчика без Docker пропускаются, и однострочная регрессия в
`PassportStore.load` (`starting_new=False`) или забытый `commit` в `await_new`
оставляли локальный прогон зелёным: `/new` без текста переставал работать, а
ничего не краснело. Здесь та же связка проверяется там, где базы нет:

* `PassportStore` — поверх подставных репозиториев, которые записывают, что их
  спросили и в каком порядке;
* репозиторий — настоящий, но поверх сессии, которая лишь записывает SQL. Этого
  хватает для всего, что дело формы запроса: предел — это `LIMIT`, порядок — это
  `ORDER BY`, а «потратить флаг атомарно» — это условный `UPDATE … RETURNING`.
  Что запрос ДЕЛАЕТ на живых строках, проверяют тесты с базой.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from sniffer.bot import store as store_module
from sniffer.bot.store import Client, Dialogue, PassportStore
from sniffer.db.repositories.passports import PassportRepository
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.domain.records import QueryOverview, StoredPassport, User
from sniffer.domain.threads import MAX_LIVE_THREADS

CLIENT = Client(tg_user_id=42, username="dima")


def bike(raw: str = "ищу скутер") -> Passport:
    return Passport(
        intent=Intent.BUY,
        category=Category.MOTORBIKE,
        city="nha_trang",
        budget=Budget(max=400, currency=Currency.USD),
        raw_query=raw,
    )


# ── PassportStore поверх подставных репозиториев ────────────────────────────


class Trace:
    """Что у репозиториев спросили и в каком порядке: порядок здесь — часть контракта."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.limits: list[int | None] = []
        self.commits = 0


class _Session:
    def __init__(self, trace: Trace) -> None:
        self._trace = trace

    async def commit(self) -> None:
        self._trace.commits += 1
        self._trace.calls.append("commit")


class _Scope:
    def __init__(self, trace: Trace) -> None:
        self._session = _Session(trace)

    async def __aenter__(self) -> _Session:
        return self._session

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _storage(
    monkeypatch: pytest.MonkeyPatch,
    *,
    armed: bool = False,
    consumable: bool = True,
    has_passport: bool = True,
    broken_insert: bool = False,
) -> tuple[PassportStore, Trace]:
    trace = Trace()
    stored = StoredPassport(id=5, user_id=7, version=1, passport=bike())

    class Users:
        def __init__(self, _session: object) -> None:
            pass

        async def get_or_create(self, tg_user_id: int, **_kwargs: object) -> User:
            return User(tg_user_id=tg_user_id, id=7, awaiting_new_request=armed)

    class Passports:
        def __init__(self, _session: object) -> None:
            pass

        async def get_current(self, _user_id: int) -> StoredPassport | None:
            return stored if has_passport else None

        async def list_events(self, _root: int) -> list[Any]:
            return []

        async def await_new_request(self, user_id: int) -> None:
            trace.calls.append(f"await_new_request:{user_id}")

        async def consume_new_request(self, user_id: int) -> bool:
            trace.calls.append(f"consume:{user_id}")
            return consumable

        async def save_new(
            self, user_id: int, passport: Passport, *, move_pointer: bool = True
        ) -> StoredPassport:
            if broken_insert:
                raise RuntimeError("база отвалилась на вставке")
            trace.calls.append(f"save_new:{user_id}")
            return StoredPassport(id=6, user_id=user_id, version=1, passport=passport)

        async def add_event(self, passport_id: int, kind: str, _payload: object = None) -> None:
            trace.calls.append(f"add_event:{passport_id}:{kind}")

        async def list_queries(self, user_id: int, *, limit: int | None = None) -> list[Any]:
            trace.calls.append(f"list_queries:{user_id}")
            trace.limits.append(limit)
            return [QueryOverview(root=1, passport=bike())]

    monkeypatch.setattr(store_module, "UserRepository", Users)
    monkeypatch.setattr(store_module, "PassportRepository", Passports)
    return PassportStore(lambda: _Scope(trace)), trace  # type: ignore[arg-type,return-value]


@pytest.mark.parametrize("armed", [True, False])
async def test_load_reads_the_armed_flag_from_the_user_row(
    monkeypatch: pytest.MonkeyPatch, armed: bool
) -> None:
    """`starting_new` — это флаг из базы, а не константа: иначе `/new` без текста не работает."""
    store, _trace = _storage(monkeypatch, armed=armed)

    dialogue = await store.load(CLIENT)

    assert dialogue.starting_new is armed


async def test_topic_selection_reads_selected_root_not_general_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = Trace()
    general = StoredPassport(id=5, user_id=7, version=1, passport=bike())
    topic = StoredPassport(id=6, user_id=7, version=1, passport=bike())

    class Passports:
        def __init__(self, _session: object) -> None:
            pass

        async def select(self, user_id: int, root: int, **_kwargs: object) -> bool:
            return user_id == 7 and root in {5, 6}

        async def get_current(self, _user_id: int) -> StoredPassport:
            return general

        async def current_of(self, _user_id: int, root: int) -> StoredPassport | None:
            return topic if root == 6 else None

        async def list_events(self, _root: int) -> list[Any]:
            return []

    class Tabs:
        def __init__(self, _session: object) -> None:
            pass

        async def root_of(self, user_id: int, thread_id: int) -> int | None:
            return 6 if user_id == 7 and thread_id == 100 else None

    monkeypatch.setattr(store_module, "PassportRepository", Passports)
    monkeypatch.setattr(store_module, "TabRepository", Tabs)
    store = PassportStore(lambda: _Scope(trace))  # type: ignore[arg-type,return-value]

    selected = await store.select(Dialogue(user_id=7, thread_id=100), 6)

    assert selected.passport is not None and selected.passport.root == 6
    wrong_topic = await store.select(Dialogue(user_id=7, passport=topic, thread_id=100), 5)
    assert wrong_topic.passport is not None and wrong_topic.passport.root == 6


async def test_await_new_writes_the_flag_and_commits_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без `commit` флаг жил бы до закрытия сессии, а следующее сообщение его не увидело бы."""
    store, trace = _storage(monkeypatch)

    await store.await_new(Dialogue(user_id=7))

    assert trace.calls == ["await_new_request:7", "commit"]


async def test_live_threads_asks_for_the_list_and_never_for_more_than_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """По длине списка бот решает, вытесняется ли поиск: без предела «мест нет» не прозвучит."""
    store, trace = _storage(monkeypatch)

    live = await store.live_threads(Dialogue(user_id=7))

    assert len(live) == 1
    assert trace.calls == ["list_queries:7"]
    assert all(limit is None or limit <= MAX_LIVE_THREADS for limit in trace.limits)


async def test_start_requested_spends_the_flag_first_and_creates_in_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сначала флаг, потом поиск, один `commit`: так проигравший не вставляет ничего."""
    store, trace = _storage(monkeypatch, armed=True)

    started = await store.start_requested(Dialogue(user_id=7, starting_new=True), bike("ищу байк"))

    assert started is not None
    assert started.passport is not None and started.passport.id == 6
    assert trace.calls == ["consume:7", "save_new:7", "add_event:6:user_message", "commit"]
    assert trace.commits == 1


async def test_start_requested_creates_nothing_when_the_flag_is_already_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проигравший гонки получает `None`: ни вставки, ни коммита, ни события."""
    store, trace = _storage(monkeypatch, armed=True, consumable=False)

    started = await store.start_requested(Dialogue(user_id=7, starting_new=True), bike())

    assert started is None
    assert trace.calls == ["consume:7"]
    assert trace.commits == 0


async def test_a_failure_after_the_flag_is_spent_does_not_commit_the_spent_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Флаг и поиск — одна транзакция: упала вставка — флаг остаётся взведённым.

    Иначе человек, чей разбор прошёл, а база моргнула на записи, терял бы `/new`, и
    следующее сообщение молча уточняло бы ПРЕЖНИЙ поиск.
    """
    store, trace = _storage(monkeypatch, armed=True, broken_insert=True)

    with pytest.raises(RuntimeError):
        await store.start_requested(Dialogue(user_id=7, starting_new=True), bike())

    assert trace.commits == 0


async def test_the_plain_start_does_not_ask_for_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Неявное рождение поиска безусловно: флага у него нет, и спрашивать его нечем."""
    store, trace = _storage(monkeypatch)

    await store.start(Dialogue(user_id=7), bike())

    assert "consume:7" not in trace.calls
    assert trace.calls == ["save_new:7", "add_event:6:user_message", "commit"]


# ── репозиторий: форма запросов без базы ────────────────────────────────────


class _Result:
    def __init__(self, scalar: object) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self) -> object:
        return self._scalar

    def first(self) -> None:
        return None

    def __iter__(self) -> Any:
        return iter(())


class RecordingSession:
    """Сессия, которая записывает SQL и ничего не исполняет."""

    def __init__(self, *, owned: bool = True, consumed: bool = True) -> None:
        self.statements: list[str] = []
        self._owned = owned
        self._consumed = consumed

    def _record(self, statement: Any) -> None:
        compiled = statement.compile(
            dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
        self.statements.append(re.sub(r"\s+", " ", str(compiled)).strip().lower())

    async def execute(self, statement: Any, *_args: object, **_kwargs: object) -> _Result:
        self._record(statement)
        return _Result(1 if self._consumed else None)

    async def scalar(self, statement: Any, *_args: object, **_kwargs: object) -> object:
        self._record(statement)
        return 1 if self._owned else None

    async def flush(self) -> None:
        return None

    def add(self, _row: object) -> None:
        return None


def _repo(**kwargs: bool) -> tuple[PassportRepository, RecordingSession]:
    session = RecordingSession(**kwargs)
    return PassportRepository(session), session  # type: ignore[arg-type]


async def test_the_flag_is_spent_by_one_conditional_update_with_returning() -> None:
    """Сравнение-и-замена в базе: `UPDATE … WHERE флаг RETURNING`, а не «прочитал — снял».

    Два сообщения после `/new` читают флаг оба, пока разбор первого идёт; только
    условный `UPDATE` отдаёт строку ровно одному из них.
    """
    repo, session = _repo()

    assert await repo.consume_new_request(7) is True

    (sql,) = session.statements
    assert sql.startswith("update users set awaiting_new_request=false")
    assert "where users.id = 7 and users.awaiting_new_request is true" in sql
    assert "returning users.id" in sql


async def test_a_flag_that_is_already_spent_reads_as_a_lost_race() -> None:
    repo, _session = _repo(consumed=False)

    assert await repo.consume_new_request(7) is False


async def test_arming_sets_the_flag_and_nothing_else() -> None:
    repo, session = _repo()

    await repo.await_new_request(7)

    (sql,) = session.statements
    assert "set awaiting_new_request=true" in sql
    assert "where users.id = 7" in sql


async def test_selecting_a_search_disarms_new_and_raises_it_in_the_list() -> None:
    """Выбор снимает `/new` и поднимает поиск в списке: на втором держится «выбор возвращает»."""
    repo, session = _repo()

    assert await repo.select(7, 99) is True

    users, passports = session.statements[1:]
    assert "awaiting_new_request=false" in users
    assert "active_passport_root=99" in users
    assert passports.startswith("update passports set last_used_at=clock_timestamp()")
    assert "passports.user_id = 7" in passports
    assert "coalesce(passports.root_id, passports.id) = 99" in passports
    assert "passports.is_current is true" in passports


async def test_selecting_a_foreign_search_changes_nothing() -> None:
    repo, session = _repo(owned=False)

    assert await repo.select(7, 99) is False

    assert len(session.statements) == 1, "после проверки владельца — ни одной записи"


async def test_the_list_is_capped_in_the_query_and_ordered_by_use() -> None:
    """Предел — `LIMIT` в SQL, порядок — по использованию, пока его нет — по созданию."""
    repo, session = _repo()

    await repo.list_queries(7)

    (sql,) = session.statements
    assert f"limit {MAX_LIVE_THREADS}" in sql
    assert (
        "order by coalesce(passports.last_used_at, passports.created_at) desc, passports.id desc"
        in sql
    )
    assert "passports.is_current is true" in sql


async def test_a_single_search_is_looked_up_by_root_without_the_list_limit() -> None:
    """Управление идёт по принадлежности: поиск ищется по корню и пределу списка не подчиняется."""
    repo, session = _repo()

    assert await repo.get_query(7, 99) is None

    (sql,) = session.statements
    assert "coalesce(passports.root_id, passports.id) = 99" in sql
    assert "passports.user_id = 7" in sql
    assert f"limit {MAX_LIVE_THREADS}" not in sql
    assert "order by" not in sql, "порядок списка для одного поиска ни к чему"
