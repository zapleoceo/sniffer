"""Повтор отказа на живом Postgres: перенос, аудит, идемпотентность, замок.

Правило «можно ли» проверяет `test_reject_retry_rules.py` без базы. Здесь — то, что
без базы не проверить: транзакционность переноса ключа, сохранение аудита, частичный
уникальный индекс и суточный лимит под КОНКУРЕНЦИЕЙ (два запроса при девяти из десяти).
Пропускается без `TEST_DATABASE_URL` (см. `conftest.py`).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sniffer.dashboard import data
from sniffer.db import collection_models, models
from sniffer.db.repositories import reject_retries
from sniffer.db.repositories.reject_retries import RejectRetryRepository
from sniffer.domain import reject_retry
from sniffer.domain.reject_retry import RetryCode, RetryResult, RetryStatus

# Таблицы сборщика регистрируются в метаданных только импортом этого модуля, а фикстура
# `db_engine` чистит ВСЕ таблицы метаданных (как в test_sql_chain_db.py).
assert collection_models

OWNER = 169510539
TEMP = "too_many_attempts"


class Boom(Exception):
    """Тип исключения, о котором код не знает: охрана не должна зависеть от списка."""


def token(index: int) -> str:
    return f"form-token-{index:04d}"


def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def reject(engine: AsyncEngine, key: str, reason: str = TEMP, *, age_h: int = 2) -> None:
    async with sessions(engine)() as session:
        session.add(
            models.ChatReject(
                key=key, reason=reason, rejected_at=datetime.now(UTC) - timedelta(hours=age_h)
            )
        )
        await session.commit()


async def history(
    engine: AsyncEngine, key: str, *, hours_ago: float, status: str = "done", index: int = 0
) -> None:
    """Прошлая попытка в журнале — без прохода через `request()`."""
    when = datetime.now(UTC) - timedelta(hours=hours_ago)
    async with sessions(engine)() as session:
        session.add(
            models.ChatRejectRetry(
                reject_key=key,
                reject_reason=TEMP,
                requested_at=when,
                requested_by=OWNER,
                idempotency_key=f"seed-{key.strip('@+')}-{index}-{int(hours_ago * 100)}",
                status=status,
                next_retry_at=when + reject_retry.COOLDOWN,
            )
        )
        await session.commit()


async def ask(engine: AsyncEngine, key: str, form: str) -> RetryResult:
    """Один запрос повтора в своей сессии и со своим коммитом — как делает `data.request_retry`."""
    async with sessions(engine)() as session:
        result = await RejectRetryRepository(session).request(
            key,
            idempotency_key=form,
            requested_by=OWNER,
            now=datetime.now(UTC),
        )
        await session.commit()
        return result


async def count(engine: AsyncEngine, model: type[models.Base]) -> int:
    async with sessions(engine)() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def has_reject(engine: AsyncEngine, key: str) -> bool:
    async with sessions(engine)() as session:
        return await session.get(models.ChatReject, key) is not None


async def queued(engine: AsyncEngine, key: str) -> models.ChatCandidate | None:
    async with sessions(engine)() as session:
        found: models.ChatCandidate | None = await session.scalar(
            select(models.ChatCandidate).where(models.ChatCandidate.key == key)
        )
        return found


# ── перенос и аудит ─────────────────────────────────────────────────────────


async def test_the_key_moves_to_the_queue_and_the_audit_row_keeps_the_original_reject(
    db_engine: AsyncEngine,
) -> None:
    await reject(db_engine, "@retry_me", age_h=5)

    result = await ask(db_engine, "@retry_me", token(1))

    assert result.status is RetryStatus.CREATED and result.record is not None
    assert not await has_reject(db_engine, "@retry_me")
    candidate = await queued(db_engine, "@retry_me")
    assert candidate is not None
    assert (candidate.status, candidate.attempts, candidate.username) == ("queued", 0, "retry_me")
    assert candidate.priority == reject_retries.RETRY_PRIORITY
    async with sessions(db_engine)() as session:
        row = await session.scalar(select(models.ChatRejectRetry))
    assert row is not None
    assert (row.reject_key, row.reject_reason, row.requested_by) == ("@retry_me", TEMP, OWNER)
    assert row.status == "active" and row.idempotency_key == token(1)
    assert row.reject_rejected_at is not None
    assert row.next_retry_at - row.requested_at == reject_retry.COOLDOWN


async def test_an_invite_key_goes_to_the_queue_as_an_invite_hash(db_engine: AsyncEngine) -> None:
    await reject(db_engine, "+AbC_d-1234")

    await ask(db_engine, "+AbC_d-1234", token(2))

    candidate = await queued(db_engine, "+AbC_d-1234")
    assert candidate is not None
    assert (candidate.invite_hash, candidate.username) == ("AbC_d-1234", None)


async def test_a_failure_in_the_middle_rolls_everything_back(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Исключение чужого типа ПОСЛЕ вставки в очередь: ни очереди, ни попытки, отказ на месте."""
    await reject(db_engine, "@atomic")

    def explode(_now: datetime) -> datetime:
        raise Boom

    monkeypatch.setattr(reject_retry, "next_retry_at", explode)
    async with sessions(db_engine)() as session:
        with pytest.raises(Boom):
            await RejectRetryRepository(session).request(
                "@atomic", idempotency_key=token(3), requested_by=OWNER, now=datetime.now(UTC)
            )
        await session.rollback()

    assert await has_reject(db_engine, "@atomic")
    assert await queued(db_engine, "@atomic") is None
    assert await count(db_engine, models.ChatRejectRetry) == 0


async def test_a_key_already_in_the_queue_is_refused_and_the_reject_stays(
    db_engine: AsyncEngine,
) -> None:
    await reject(db_engine, "@twice")
    async with sessions(db_engine)() as session:
        session.add(models.ChatCandidate(key="@twice", username="twice"))
        await session.commit()

    result = await ask(db_engine, "@twice", token(4))

    assert result.decision.code is RetryCode.ALREADY_QUEUED
    assert await has_reject(db_engine, "@twice")
    assert await count(db_engine, models.ChatRejectRetry) == 0


# ── идемпотентность ─────────────────────────────────────────────────────────


async def test_the_same_form_twice_creates_one_attempt(db_engine: AsyncEngine) -> None:
    await reject(db_engine, "@once")

    first = await ask(db_engine, "@once", token(5))
    second = await ask(db_engine, "@once", token(5))

    assert (first.status, second.status) == (RetryStatus.CREATED, RetryStatus.REPLAYED)
    assert second.record is not None and first.record is not None
    assert second.record.id == first.record.id
    assert await count(db_engine, models.ChatRejectRetry) == 1


async def test_another_form_for_a_key_that_is_already_retrying_is_refused(
    db_engine: AsyncEngine,
) -> None:
    await reject(db_engine, "@busy")
    await ask(db_engine, "@busy", token(6))

    other = await ask(db_engine, "@busy", token(7))

    assert other.decision.code is RetryCode.ALREADY_QUEUED
    assert await count(db_engine, models.ChatRejectRetry) == 1


async def test_a_form_token_cannot_be_reused_for_another_key(db_engine: AsyncEngine) -> None:
    await reject(db_engine, "@first")
    await reject(db_engine, "@second")
    await ask(db_engine, "@first", token(8))

    stolen = await ask(db_engine, "@second", token(8))

    assert stolen.decision.code is RetryCode.BAD_KEY
    assert await has_reject(db_engine, "@second")


async def test_the_database_itself_refuses_two_active_attempts_for_one_key(
    db_engine: AsyncEngine,
) -> None:
    """Частичный уникальный индекс держит инвариант даже мимо репозитория."""
    await history(db_engine, "@dup", hours_ago=1, status="active", index=1)

    with pytest.raises(IntegrityError):
        await history(db_engine, "@dup", hours_ago=2, status="active", index=2)


# ── правила под замком ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("user", RetryCode.PERMANENT),
        ("already_member", RetryCode.MEMBERSHIP),
        ("join_request_sent", RetryCode.PENDING),
        ("unresolved", RetryCode.UNKNOWN),
        ("brand_new_code", RetryCode.UNKNOWN),
    ],
)
async def test_only_a_temporary_reject_can_be_retried(
    db_engine: AsyncEngine, reason: str, code: RetryCode
) -> None:
    await reject(db_engine, "@nope", reason)

    result = await ask(db_engine, "@nope", token(9))

    assert result.status is RetryStatus.REFUSED and result.decision.code is code
    assert await has_reject(db_engine, "@nope")
    assert await queued(db_engine, "@nope") is None
    assert await count(db_engine, models.ChatRejectRetry) == 0


async def test_cooldown_and_the_per_key_ceiling_are_enforced_from_the_journal(
    db_engine: AsyncEngine,
) -> None:
    await reject(db_engine, "@cool")
    await history(db_engine, "@cool", hours_ago=3)
    assert (await ask(db_engine, "@cool", token(10))).decision.code is RetryCode.COOLDOWN

    await history(db_engine, "@cool", hours_ago=30, index=2)
    await history(db_engine, "@cool", hours_ago=50, index=3)
    # три попытки в журнале: даже если cooldown давно вышел, потолок на ключ
    async with sessions(db_engine)() as session:
        await session.execute(
            update(models.ChatRejectRetry).values(
                requested_at=datetime.now(UTC) - timedelta(hours=40)
            )
        )
        await session.commit()
    assert (await ask(db_engine, "@cool", token(11))).decision.code is RetryCode.ATTEMPTS_EXHAUSTED


async def test_a_second_attempt_is_allowed_after_the_cooldown(db_engine: AsyncEngine) -> None:
    await reject(db_engine, "@later")
    await history(db_engine, "@later", hours_ago=25)

    result = await ask(db_engine, "@later", token(12))

    assert result.status is RetryStatus.CREATED
    assert await count(db_engine, models.ChatRejectRetry) == 2


async def test_a_flood_stop_blocks_the_retry_until_it_ends(db_engine: AsyncEngine) -> None:
    await reject(db_engine, "@flooded")
    now = datetime.now(UTC)
    async with sessions(db_engine)() as session:
        session.add(
            models.ChatJoinEvent(
                kind="flood", happened_at=now, blocked_until=now + timedelta(hours=5)
            )
        )
        await session.commit()

    blocked = await ask(db_engine, "@flooded", token(13))
    assert blocked.decision.code is RetryCode.FLOOD_STOP
    assert blocked.decision.available_at is not None
    assert await has_reject(db_engine, "@flooded")

    async with sessions(db_engine)() as session:
        await session.execute(
            update(models.ChatJoinEvent).values(blocked_until=now - timedelta(minutes=1))
        )
        await session.commit()
    assert (await ask(db_engine, "@flooded", token(14))).status is RetryStatus.CREATED


async def test_missing_and_malformed_keys_get_their_own_answers(db_engine: AsyncEngine) -> None:
    assert (await ask(db_engine, "@ghost", token(15))).decision.code is RetryCode.NOT_FOUND
    assert (await ask(db_engine, "no-at-sign", token(16))).decision.code is RetryCode.BAD_KEY
    assert (await ask(db_engine, "@ok_key", "short")).decision.code is RetryCode.BAD_KEY


# ── суточный лимит и конкуренция ────────────────────────────────────────────


async def spend_daily(engine: AsyncEngine, used: int) -> None:
    for index in range(used):
        await history(engine, f"@spent{index}", hours_ago=1 + index / 10, index=index)


async def test_the_daily_limit_refuses_the_eleventh_request(db_engine: AsyncEngine) -> None:
    await spend_daily(db_engine, reject_retry.MAX_RETRIES_PER_DAY)
    await reject(db_engine, "@eleventh")

    result = await ask(db_engine, "@eleventh", token(17))

    assert result.decision.code is RetryCode.DAILY_LIMIT
    assert result.decision.available_at is not None
    assert await has_reject(db_engine, "@eleventh")


async def race(
    engine: AsyncEngine, first: tuple[str, str], second: tuple[str, str]
) -> tuple[RetryResult, RetryResult]:
    """Два запроса внахлёст, без надежды на везение планировщика.

    Первый уже внутри транзакции и НЕ закоммичен, когда стартует второй. Без замка
    второй не видит чужих незакоммиченных строк, считает те же девять и проходит;
    с замком он стоит на нём, пока первый не закоммитится, и считает уже десять.
    Простой `gather` этого не гарантировал: убрав замок, тест оставался зелёным.
    """
    now = datetime.now(UTC)
    async with sessions(engine)() as holder:
        done_first = await RejectRetryRepository(holder).request(
            first[0], idempotency_key=first[1], requested_by=OWNER, now=now
        )
        pending = asyncio.create_task(ask(engine, *second))
        await asyncio.sleep(0.5)  # второй успел дойти до замка (или проскочить без него)
        await holder.commit()
    return done_first, await pending


async def test_two_parallel_requests_at_nine_used_let_exactly_one_through(
    db_engine: AsyncEngine,
) -> None:
    """Девять из десяти использованы, двое одновременно: проходит ровно один."""
    await spend_daily(db_engine, reject_retry.MAX_RETRIES_PER_DAY - 1)
    await reject(db_engine, "@race_a")
    await reject(db_engine, "@race_b")

    results = await race(db_engine, ("@race_a", token(18)), ("@race_b", token(19)))

    codes = sorted(result.decision.code.value for result in results)
    assert codes == [RetryCode.DAILY_LIMIT.value, RetryCode.OK.value]
    assert await count(db_engine, models.ChatRejectRetry) == reject_retry.MAX_RETRIES_PER_DAY
    kept = [await has_reject(db_engine, "@race_a"), await has_reject(db_engine, "@race_b")]
    assert sorted(kept) == [False, True], "перенесён ровно один ключ"


async def test_the_same_form_sent_twice_at_once_creates_one_attempt(db_engine: AsyncEngine) -> None:
    """Двойной клик: второй POST той же формы приходит, пока первый ещё не закоммичен."""
    await reject(db_engine, "@dblclick")

    results = await race(db_engine, ("@dblclick", token(20)), ("@dblclick", token(20)))

    assert [result.status for result in results] == [RetryStatus.CREATED, RetryStatus.REPLAYED]
    assert await count(db_engine, models.ChatRejectRetry) == 1


# ── что страница видит и как закрывается попытка ────────────────────────────


async def test_the_offer_for_the_page_is_the_decision_the_server_will_make(
    db_engine: AsyncEngine,
) -> None:
    await reject(db_engine, "@offer")
    await reject(db_engine, "@offer_user", "user")
    await history(db_engine, "@offer", hours_ago=3)
    now = datetime.now(UTC)

    async with sessions(db_engine)() as session:
        offers = await RejectRetryRepository(session).offers(
            [("@offer", TEMP), ("@offer_user", "user")], now, None
        )

    assert set(offers) == {"@offer"}, "кнопка бывает только у временного класса"
    assert offers["@offer"].decision.code is RetryCode.COOLDOWN
    assert offers["@offer"].attempts_used == 1
    assert (await ask(db_engine, "@offer", token(21))).decision.code is RetryCode.COOLDOWN


async def test_a_finished_attempt_is_settled_with_the_outcome_read_from_the_state(
    db_engine: AsyncEngine,
) -> None:
    await reject(db_engine, "@joined")
    await reject(db_engine, "@again")
    await ask(db_engine, "@joined", token(22))
    await ask(db_engine, "@again", token(23))
    async with sessions(db_engine)() as session:
        # joiner вступил в первый и снял его из очереди; второй отклонил заново
        await session.execute(
            delete(models.ChatCandidate).where(models.ChatCandidate.key.in_(["@joined", "@again"]))
        )
        session.add(models.ChatReject(key="@again", reason=TEMP))
        await session.commit()

    async with sessions(db_engine)() as session:
        shown = {
            record.reject_key: record for record in await RejectRetryRepository(session).recent()
        }
    assert (shown["@joined"].status, shown["@joined"].outcome) == ("done", "left_queue")
    assert (shown["@again"].status, shown["@again"].outcome) == ("done", "rejected_again")

    async with sessions(db_engine)() as session:
        stored = await RejectRetryRepository(session).settle_finished(datetime.now(UTC))
        await session.commit()
    assert stored == 2
    async with sessions(db_engine)() as session:
        statuses = set(await session.scalars(select(models.ChatRejectRetry.status)))
    assert statuses == {"done"}


async def test_an_attempt_whose_key_is_still_queued_stays_active(db_engine: AsyncEngine) -> None:
    await reject(db_engine, "@waiting")
    await ask(db_engine, "@waiting", token(24))

    async with sessions(db_engine)() as session:
        records = await RejectRetryRepository(session).recent()
        assert await RejectRetryRepository(session).settle_finished(datetime.now(UTC)) == 0

    assert [(r.reject_key, r.status, r.outcome) for r in records] == [("@waiting", "active", None)]


# ── граница дашборда: то, что реально вызывает страница ─────────────────────


@pytest.fixture
def dashboard_db(db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> AsyncEngine:
    """`data.py` ходит в базу через `session_scope`: направляем его на тестовую."""

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with sessions(db_engine)() as session:
            yield session

    monkeypatch.setattr(data, "session_scope", scope)
    return db_engine


async def test_the_inventory_snapshot_carries_offers_counters_and_the_real_chat_count(
    dashboard_db: AsyncEngine,
) -> None:
    await reject(dashboard_db, "@snap_temp")
    await reject(dashboard_db, "@snap_perm", "user")
    await history(dashboard_db, "@older", hours_ago=2)
    async with sessions(dashboard_db)() as session:
        session.add(models.Chat(tg_id=-100123, title="Нячанг барахолка", city="nha-trang"))
        await session.commit()

    view = await data.inventory()

    assert [item.key for item in view.temporary_rejects] == ["@snap_temp"]
    assert (
        set(view.retry_offers) == {"@snap_temp"}
        and view.retry_offers["@snap_temp"].decision.allowed
    )
    assert (view.retries_today, view.tracked_chats) == (1, 1)
    assert [record.reject_key for record in view.retries] == ["@older"]


async def test_request_retry_commits_what_it_did_even_when_it_refuses(
    dashboard_db: AsyncEngine,
) -> None:
    await reject(dashboard_db, "@via_data")
    await reject(dashboard_db, "@via_data_user", "user")

    created = await data.request_retry("@via_data", idempotency_key=token(30), requested_by=OWNER)
    refused = await data.request_retry(
        "@via_data_user", idempotency_key=token(31), requested_by=OWNER
    )

    assert created.status is RetryStatus.CREATED and refused.decision.code is RetryCode.PERMANENT
    assert await queued(dashboard_db, "@via_data") is not None
    assert await has_reject(dashboard_db, "@via_data_user")
