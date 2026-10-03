"""Форма запросов квоты: страховка там, где живой Postgres недоступен.

Поведение под блокировкой проверяет только настоящая база (`test_quota_db.py`:
гонки, барьеры, формула). Но у «живых» тестов есть слабость: без
`TEST_DATABASE_URL` они молча пропускаются, и удалённая блокировка выглядит
зелёным прогоном. Поэтому здесь — запись SQL, который репозиторий отправляет
базе, на подставной сессии: что идёт ДО чего и чем ограничен каждый оператор.
Проверка грубая намеренно и ломается только вместе с самим правилом:
порядок «блокировка → чтение → вставка» и условия `IS NULL` — это не стиль
запроса, а то, на чём держится «ровно десять».
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from sniffer.db.repositories.quota import QuotaRepository
from sniffer.db.repositories.users import UserRepository
from sniffer.domain.quota import Channel, Claim, Ticket

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
ANCHOR = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
# Диалект боевого драйвера без соединения: движок создаётся лениво и к базе не ходит.
DIALECT = create_async_engine("postgresql+asyncpg://sniffer:sniffer@localhost/shape").dialect


class Rows:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def one_or_none(self) -> Any:
        return self.rows[0] if self.rows else None

    def scalars(self) -> Rows:
        return self

    def all(self) -> list[Any]:
        return self.rows

    def __iter__(self) -> Any:
        return iter(self.rows)


class Recorder:
    """Подставная сессия: пишет SQL по порядку и отвечает ровно настолько, чтобы код дошёл."""

    def __init__(
        self,
        *,
        anchor: datetime | None = ANCHOR,
        lost_race: bool = False,
        seen: tuple[int, ...] = (),
        used: int = 0,
        inserted: tuple[int, ...] | None = None,
    ) -> None:
        self.anchor, self.lost_race, self.seen, self.used = anchor, lost_race, seen, used
        self.inserted = inserted
        self.sql: list[str] = []

    def _text(self, statement: Any) -> str:
        compiled = statement.compile(dialect=DIALECT, compile_kwargs={"literal_binds": True})
        sql = " ".join(str(compiled).split())
        self.sql.append(sql)
        return sql

    async def execute(self, statement: Any) -> Rows:
        sql = self._text(statement)
        if sql.startswith("SELECT users.id, users.quota_anchor_at"):
            return Rows([(7, self.anchor)])
        if sql.startswith("SELECT listings.id"):
            return Rows([])
        return Rows([1])

    async def scalar(self, statement: Any) -> Any:
        sql = self._text(statement)
        if sql.startswith("UPDATE users SET quota_anchor_at"):
            return None if self.lost_race else T0
        if sql.startswith("UPDATE users SET paywall_offered_at"):
            return 7
        if "FOR UPDATE" in sql:
            return 11
        if "count(" in sql:
            return self.used
        if sql.startswith("SELECT users.quota_anchor_at"):
            return ANCHOR
        return 7

    async def scalars(self, statement: Any) -> Rows:
        sql = self._text(statement)
        if sql.startswith("INSERT INTO offer_views"):
            if self.inserted is not None:
                return Rows(list(self.inserted))
            params = statement.compile(dialect=DIALECT).params
            return Rows([v for k, v in params.items() if k.startswith("listing_id")])
        if sql.startswith("SELECT offer_views.listing_id"):
            return Rows(list(self.seen))
        return Rows([])

    def first(self, prefix: str) -> int:
        return next(i for i, sql in enumerate(self.sql) if sql.startswith(prefix))

    def only(self, prefix: str) -> str:
        found = [sql for sql in self.sql if sql.startswith(prefix)]
        assert len(found) == 1, (prefix, found)
        return found[0]


def claim(ids: tuple[int, ...] = (1, 2, 3), **kwargs: Any) -> Claim:
    fields: dict[str, Any] = {
        "user_id": 7,
        "listing_ids": ids,
        "channel": Channel.SEARCH,
        "now": T0,
        "limit": 10,
        "request_id": 9,
    }
    fields.update(kwargs)
    return Claim(**fields)


async def reserved(**kwargs: Any) -> Recorder:
    session = Recorder(**kwargs)
    await QuotaRepository(session).reserve(claim())  # type: ignore[arg-type]
    return session


# ── порядок: блокировка → чтение → решение → вставка ────────────────────────


async def test_the_period_row_is_locked_before_anything_is_read_or_written() -> None:
    """Без блокировки двадцать ответов разом увидели бы одно и то же «занято»."""
    session = await reserved()

    lock = session.first("SELECT quota_periods.id")
    assert "FOR UPDATE" in session.sql[lock]
    assert lock < session.first("SELECT offer_views.listing_id"), "виденное читается под замком"
    assert lock < session.first("SELECT count(*)"), "занятое считается под замком"
    assert lock < session.first("INSERT INTO offer_views"), "вставка — под замком"


async def test_what_is_already_in_the_period_is_read_before_the_decision() -> None:
    """Иначе «виденное» считалось бы дважды: решение не знало бы, что оно уже оплачено."""
    session = await reserved()

    assert session.first("SELECT offer_views.listing_id") < session.first("INSERT INTO offer_views")
    assert session.first("SELECT count(*)") < session.first("INSERT INTO offer_views")


async def test_the_lock_is_taken_on_the_users_own_period_number() -> None:
    session = await reserved()

    lock = session.sql[session.first("SELECT quota_periods.id")]
    assert "quota_periods.user_id = 7" in lock and "quota_periods.period_no = 0" in lock


# ── условия, на которых держатся «один раз» и «ровно столько» ────────────────


async def test_the_anchor_is_claimed_only_while_it_is_empty() -> None:
    """Якорь не перезаписывается: условие `IS NULL` — и есть «ставится один раз»."""
    session = await reserved(anchor=None)

    assert "quota_anchor_at IS NULL" in session.only("UPDATE users SET quota_anchor_at")


async def test_an_existing_anchor_is_never_written_again() -> None:
    session = await reserved(anchor=ANCHOR)

    assert not any(sql.startswith("UPDATE users SET quota_anchor_at") for sql in session.sql)


async def test_a_lost_race_for_the_anchor_takes_the_winners_value() -> None:
    """Условный UPDATE дождался чужого коммита и ничего не тронул: якорь теперь чужой."""
    session = await reserved(anchor=None, lost_race=True)

    period = session.only("INSERT INTO quota_periods")
    assert "2026-09-30 12:00:00" in period, (
        "период считается от якоря победителя, не от своего «сейчас»"
    )
    assert "2026-10-03 12:00:00+00:00', 0" not in period


async def test_the_card_insert_conflicts_on_the_unique_pair_and_does_nothing() -> None:
    session = await reserved()

    assert "ON CONFLICT (period_id, listing_id) DO NOTHING" in session.only(
        "INSERT INTO offer_views"
    )


async def test_the_period_is_created_lazily_and_conflicts_on_its_number() -> None:
    session = await reserved()

    assert "ON CONFLICT (user_id, period_no) DO NOTHING" in session.only(
        "INSERT INTO quota_periods"
    )


async def test_only_dialog_channels_are_counted_as_spent() -> None:
    session = await reserved()

    assert "channel != 'monitor'" in session.only("SELECT count(*)")


async def test_a_card_that_was_not_recorded_is_a_loud_failure_not_a_free_card() -> None:
    """Под замком конфликта быть не может; если он случился, бесплатно показывать нельзя."""
    with pytest.raises(RuntimeError, match="блокировка периода не удержана"):
        await reserved(inserted=(1,))


async def test_an_empty_list_is_refused_before_any_sql() -> None:
    session = Recorder()

    with pytest.raises(ValueError, match="пуст"):
        await QuotaRepository(session).reserve(claim(()))  # type: ignore[arg-type]
    assert session.sql == []


# ── подтверждение, возврат, чистка ──────────────────────────────────────────

TICKET = Ticket(user_id=7, period_id=11, granted=(1, 2), shown=(1, 2, 3), request_id=9)


async def test_release_removes_only_the_unconfirmed_rows_of_its_own_period() -> None:
    session = Recorder()

    await QuotaRepository(session).release(TICKET)  # type: ignore[arg-type]

    sql = session.only("DELETE FROM offer_views")
    assert "delivered_at IS NULL" in sql, "подтверждённое откатом не стирается"
    assert "period_id = 11" in sql and "user_id = 7" in sql
    assert "listing_id IN (1, 2)" in sql, "только свои новые, не виденные ранее"


async def test_confirm_never_moves_an_earlier_confirmation() -> None:
    session = Recorder()

    await QuotaRepository(session).confirm(TICKET, at=T0)  # type: ignore[arg-type]

    assert "delivered_at IS NULL" in session.only("UPDATE offer_views SET delivered_at")
    assert "shown_count" in session.only("UPDATE client_requests SET shown_count")


async def test_the_sweep_skips_locked_rows_and_spares_the_monitor() -> None:
    session = Recorder()

    await QuotaRepository(session).sweep_stale(older_than=T0, limit=100)  # type: ignore[arg-type]

    sql = session.only("DELETE FROM offer_views")
    assert "SKIP LOCKED" in sql
    assert "delivered_at IS NULL" in sql
    assert "channel IN ('search', 'deferred')" in sql, "резервы слежения подтверждает нотификатор"


async def test_the_offer_claim_is_one_conditional_update() -> None:
    session = Recorder()

    await UserRepository(session).claim_paywall_offer(  # type: ignore[arg-type]
        7, now=T0, cooldown=timedelta(days=1)
    )

    sql = session.only("UPDATE users SET paywall_offered_at")
    assert "paywall_offered_at IS NULL OR" in sql and "2026-10-02 12:00:00" in sql


async def test_identifying_cards_is_one_read_by_the_pair_the_source_uses() -> None:
    session = Recorder()

    await QuotaRepository(session).identify([("chotot", "1"), ("archive", "2")])  # type: ignore[arg-type]

    sql = session.only("SELECT listings.id")
    assert "(listings.source, listings.external_id) IN (" in sql
    assert "'chotot', '1'" in sql and "'archive', '2'" in sql


async def test_identifying_nothing_does_not_touch_the_database() -> None:
    session = Recorder()

    assert await QuotaRepository(session).identify([]) == {}  # type: ignore[arg-type]
    assert session.sql == []


async def test_reading_the_standing_writes_nothing() -> None:
    session = Recorder()

    await QuotaRepository(session).usage(7, T0)  # type: ignore[arg-type]

    assert session.sql and all(sql.startswith("SELECT") for sql in session.sql)


# ── решение действительно опирается на прочитанное ──────────────────────────


def inserted_ids(session: Recorder) -> list[int]:
    insert = session.only("INSERT INTO offer_views")
    return [int(part) for part in re.findall(r"VALUES \(7, 11, (\d+),", insert)] + [
        int(part) for part in re.findall(r"\), \(7, 11, (\d+),", insert)
    ]


async def test_a_card_seen_in_the_period_is_not_inserted_again_but_counted_as_a_view() -> None:
    session = await reserved(seen=(1,))

    assert sorted(inserted_ids(session)) == [2, 3]
    assert "times_shown=(offer_views.times_shown + 1)" in session.only(
        "UPDATE offer_views SET times_shown"
    )


async def test_the_remainder_comes_from_what_is_already_spent_in_the_period() -> None:
    session = await reserved(used=9)

    assert inserted_ids(session) == [1], "осталась одна карточка из десяти"
    assert "withheld_count=(client_requests.withheld_count + 2)" in session.only(
        "UPDATE client_requests SET withheld_count"
    )
