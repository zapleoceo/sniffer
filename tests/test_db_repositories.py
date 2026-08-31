"""Репозитории на живом Postgres.

Пропускаются, пока не задан `TEST_DATABASE_URL` (см. `conftest.py`): проверять
`ON CONFLICT DO NOTHING`, `FOR UPDATE SKIP LOCKED` и `TEXT[]` не на Postgres
бессмысленно — именно эти места и ломаются, а на подделке они зелёные всегда.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sniffer.db.repositories import (
    ChatRepository,
    JobRepository,
    ListingRepository,
    PassportRepository,
    RawMessageRepository,
    UserRepository,
)
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.domain.records import Chat, Listing, RawMessage

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL не задан: живого Postgres нет",
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _raw(msg_id: int, chat_tg_id: int = -100123) -> RawMessage:
    return RawMessage(
        chat_tg_id=chat_tg_id,
        msg_id=msg_id,
        text=f"Продам Honda Vision, 7 триеу, сообщение {msg_id}",
        text_hash=f"hash-{msg_id}",
        posted_at=NOW,
    )


# ── чаты ────────────────────────────────────────────────────────────────────


async def test_chat_add_and_read_back(db_session: AsyncSession) -> None:
    repo = ChatRepository(db_session)
    stored = await repo.add(
        Chat(tg_id=-100123, title="Нячанг байки", city="nha_trang", categories=["motorbike"])
    )
    await db_session.commit()

    assert stored.id is not None
    found = await repo.get_by_tg_id(-100123)
    assert found is not None
    assert found.categories == ["motorbike"], "TEXT[] должен вернуться списком, а не строкой"


async def test_active_chats_come_by_rank(db_session: AsyncSession) -> None:
    repo = ChatRepository(db_session)
    await repo.add(Chat(tg_id=1, title="плотный", city="nha_trang", search_rank=10))
    await repo.add(Chat(tg_id=2, title="редкий", city="nha_trang", search_rank=90))
    await repo.add(Chat(tg_id=3, title="выключен", city="nha_trang", is_active=False))
    await db_session.commit()

    assert [chat.tg_id for chat in await repo.list_active()] == [1, 2]


async def test_sync_cursor_only_moves_forward(db_session: AsyncSession) -> None:
    repo = ChatRepository(db_session)
    await repo.add(Chat(tg_id=-100123, title="чат", city="nha_trang"))
    await repo.mark_synced(-100123, 500)
    await repo.mark_synced(-100123, 300)
    await db_session.commit()

    chat = await repo.get_by_tg_id(-100123)
    assert chat is not None
    assert chat.last_msg_id == 500
    assert chat.last_synced_at is not None


# ── сырьё ───────────────────────────────────────────────────────────────────


async def test_batch_insert_skips_already_seen(db_session: AsyncSession) -> None:
    """Коллектор перечитывает историю после простоя — повтор обязан быть дешёвым."""
    repo = RawMessageRepository(db_session)
    first = await repo.add_many([_raw(1), _raw(2)])
    await db_session.commit()

    again = await repo.add_many([_raw(1), _raw(2), _raw(3)])
    await db_session.commit()

    assert len(first) == 2
    assert len(again) == 1, "новым было только третье сообщение"


async def test_batch_insert_of_nothing_is_not_a_query(db_session: AsyncSession) -> None:
    assert await RawMessageRepository(db_session).add_many([]) == []


async def test_raw_message_read_by_key(db_session: AsyncSession) -> None:
    repo = RawMessageRepository(db_session)
    await repo.add_many([_raw(1)])
    await db_session.commit()

    found = await repo.get_by_key(-100123, 1)
    assert found is not None
    assert found.stage == "pending"
    assert found.gate_signals == {}
    assert await repo.get_by_key(-100123, 42) is None


async def test_stage_moves_the_message_along_the_funnel(db_session: AsyncSession) -> None:
    repo = RawMessageRepository(db_session)
    ids = await repo.add_many([_raw(1), _raw(2)])
    await repo.set_stage(ids[:1], "gated")
    await db_session.commit()

    assert len(await repo.list_by_stage("pending")) == 1
    assert len(await repo.list_by_stage("gated")) == 1


# ── карточки ────────────────────────────────────────────────────────────────


async def test_listing_survives_the_round_trip(db_session: AsyncSession) -> None:
    raw_ids = await RawMessageRepository(db_session).add_many([_raw(1)])
    stored = await ListingRepository(db_session).add(
        Listing(
            raw_message_id=raw_ids[0],
            deal_type="sell",
            category="motorbike",
            city="nha_trang",
            title="Honda Vision 2021",
            summary="Автомат, документы есть",
            tg_link="https://t.me/c/100123/1",
            posted_at=NOW,
            price_amount=Decimal("7000000.00"),
            price_currency="VND",
            price_period="once",
            price_usd_month=Decimal("280.00"),
            attributes={"transmission": "auto", "year": 2021},
            confidence=0.9,
        )
    )
    await db_session.commit()

    repo = ListingRepository(db_session)
    assert stored.id is not None
    by_id = await repo.get(stored.id)
    assert by_id is not None
    assert by_id.price_amount == Decimal("7000000.00")
    assert by_id.attributes == {"transmission": "auto", "year": 2021}

    by_raw = await repo.get_by_raw_message(raw_ids[0])
    assert by_raw is not None and by_raw.id == stored.id
    assert await repo.get_by_raw_message(raw_ids[0] + 999) is None


# ── клиенты и паспорта ──────────────────────────────────────────────────────


async def test_user_is_created_once(db_session: AsyncSession) -> None:
    repo = UserRepository(db_session)
    first = await repo.get_or_create(42, username="dima")
    await db_session.commit()
    again = await repo.get_or_create(42, username="dima")
    await db_session.commit()

    assert first.id == again.id
    assert await repo.get_by_tg_id(43) is None


def _passport(budget_max: float) -> Passport:
    return Passport(
        intent=Intent.RENT,
        category=Category.APARTMENT,
        city="nha_trang",
        districts=["Vinh Hai"],
        budget=Budget(max=budget_max, currency=Currency.USD),
        raw_query="студия у моря",
    )


async def test_passport_revision_keeps_history(db_session: AsyncSession) -> None:
    """«Дорого» создаёт версию, а не переписывает прежнюю."""
    user = await UserRepository(db_session).get_or_create(42)
    assert user.id is not None

    repo = PassportRepository(db_session)
    first = await repo.save_new(user.id, _passport(400))
    second = await repo.save_revision(first, _passport(300))
    await db_session.commit()

    assert (first.version, second.version) == (1, 2)
    assert second.root == first.id, "новая версия остаётся в цепочке первой"

    kept = await repo.get(first.id)
    assert kept is not None
    assert kept.is_current is False
    assert kept.passport.budget.max == 400

    current = await repo.get_current(user.id)
    assert current is not None
    assert current.id == second.id
    assert current.passport.budget.max == 300
    assert current.passport.districts == ["Vinh Hai"]


# ── очередь ─────────────────────────────────────────────────────────────────


async def test_taken_job_counts_the_attempt(db_session: AsyncSession) -> None:
    repo = JobRepository(db_session)
    await repo.enqueue("extract", {"raw_id": 1})
    await db_session.commit()

    job = await repo.take()
    await db_session.commit()
    assert job is not None
    assert (job.kind, job.payload, job.attempts) == ("extract", {"raw_id": 1}, 1)

    assert await repo.take() is None, "взятая задача не выдаётся второй раз"


async def test_job_from_the_future_waits(db_session: AsyncSession) -> None:
    repo = JobRepository(db_session)
    await repo.enqueue("later", run_after=datetime.now(UTC) + timedelta(hours=1))
    await db_session.commit()

    assert await repo.take() is None


async def test_take_filters_by_kind(db_session: AsyncSession) -> None:
    repo = JobRepository(db_session)
    await repo.enqueue("extract")
    await repo.enqueue("notify")
    await db_session.commit()

    job = await repo.take(kinds=["notify"])
    assert job is not None and job.kind == "notify"


async def test_failed_job_returns_to_the_queue_later(db_session: AsyncSession) -> None:
    repo = JobRepository(db_session)
    job_id = await repo.enqueue("extract")
    await db_session.commit()

    await repo.take()
    await repo.fail(job_id, "брокер не ответил", retry_in=timedelta(minutes=5))
    await db_session.commit()
    assert await repo.take() is None, "повтор назначен на потом"

    await repo.fail(job_id, "и во второй раз", retry_in=None)
    await db_session.commit()
    assert await repo.take() is None, "похороненная задача не воскресает"


async def test_finished_job_leaves_the_queue(db_session: AsyncSession) -> None:
    repo = JobRepository(db_session)
    job_id = await repo.enqueue("extract")
    await db_session.commit()

    await repo.take()
    await repo.finish(job_id)
    await db_session.commit()

    assert await repo.take() is None


async def test_two_workers_take_different_jobs(db_engine: AsyncEngine) -> None:
    """Ради этого очередь и живёт в Postgres (architecture.md, раздел 3).

    Второй воркер не ждёт освобождения строки, а пропускает её и берёт
    следующую — иначе четыре процесса выстроились бы в очередь за одной
    задачей.
    """
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as setup:
        await JobRepository(setup).enqueue("extract", {"n": 1})
        await JobRepository(setup).enqueue("extract", {"n": 2})
        await setup.commit()

    # Транзакция первого воркера намеренно не закрыта, пока второй берёт своё.
    async with sessions() as first, sessions() as second:
        taken_first = await JobRepository(first).take()
        taken_second = await JobRepository(second).take()
        await first.commit()
        await second.commit()

    assert taken_first is not None and taken_second is not None
    assert taken_first.id != taken_second.id
