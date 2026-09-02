"""Репозитории на живом Postgres.

Пропускаются, пока не задан `TEST_DATABASE_URL` (см. `conftest.py`): проверять
`ON CONFLICT DO NOTHING`, `FOR UPDATE SKIP LOCKED` и `TEXT[]` не на Postgres
бессмысленно — именно эти места и ломаются, а на подделке они зелёные всегда.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sniffer.bot.store import Client, PassportStore
from sniffer.db import models
from sniffer.db.repositories import (
    CandidateRepository,
    ChatRepository,
    JobRepository,
    JoinLedgerRepository,
    ListingRepository,
    PassportRepository,
    RawMessageRepository,
    RejectRepository,
    UserRepository,
)
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.domain.records import Chat, DiscoveryCandidate, Listing, Payment, RawMessage
from sniffer.pipeline.gate import GateResult

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


# ── безопасная очередь вступления ──────────────────────────────────────────


async def test_candidate_queue_reserves_in_priority_order_and_counts_unknown_attempts(
    db_session: AsyncSession,
) -> None:
    repo = CandidateRepository(db_session)
    await repo.push(DiscoveryCandidate(key="@later", username="later", priority=20))
    await repo.push(DiscoveryCandidate(key="@first", username="first", priority=10))
    await db_session.commit()

    first = await repo.reserve()
    await db_session.commit()
    assert first is not None and first.key == "@first"
    assert await repo.release(first.key) == 1
    await db_session.commit()

    # Освобождённый кандидат остаётся первым, но попытка хранится в БД.
    again = await repo.reserve()
    assert again is not None and again.key == "@first"
    await db_session.commit()


async def test_join_ledger_never_claims_an_eleventh_slot_in_a_rolling_day(
    db_session: AsyncSession,
) -> None:
    repo = JoinLedgerRepository(db_session)
    moments = [NOW + timedelta(hours=hour) for hour in range(0, 20, 2)]
    for moment in moments:
        assert (
            await repo.claim_slot(
                moment,
                window=timedelta(hours=24),
                maximum=10,
                next_allowed_at=moment + timedelta(hours=1),
            )
            is not None
        )
        await db_session.commit()

    assert (
        await repo.claim_slot(
            NOW + timedelta(hours=20),
            window=timedelta(hours=24),
            maximum=10,
            next_allowed_at=NOW + timedelta(hours=21),
        )
        is None
    )


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


async def test_two_parallel_revisions_do_not_share_a_version(db_engine: AsyncEngine) -> None:
    """Двойной тап по кнопке не должен дать две версии с одним номером.

    `Barrier` здесь обязателен: без него воркеры уходят в базу по очереди,
    гонка не наступает вовсе, и тест зеленеет на сломанном коде. С ним оба
    запроса стартуют одновременно — и без блокировки корня цепочки каждый
    считает следующий номер от одной и той же прежней версии, а `is_current`
    остаётся у обоих: дальше подписка и аналитика видят два «сейчас».
    """
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as setup:
        user = await UserRepository(setup).get_or_create(42)
        assert user.id is not None
        first = await PassportRepository(setup).save_new(user.id, _passport(400))
        await setup.commit()

    gate = asyncio.Barrier(2)

    async def revise(budget_max: float) -> None:
        async with sessions() as session:
            await gate.wait()
            await PassportRepository(session).save_revision(first, _passport(budget_max))
            await session.commit()

    await asyncio.gather(revise(300), revise(200))

    async with sessions() as check:
        chain = await PassportRepository(check).list_versions(first.id)

    assert [row.version for row in chain] == [1, 2, 3], "номера версий не повторяются"
    assert [row.is_current for row in chain] == [False, False, True], "актуальная — ровно одна"


async def test_events_of_the_whole_chain_come_in_order(db_session: AsyncSession) -> None:
    """Ответ клиента меняет версию — счётчик заданных вопросов продолжается."""
    user = await UserRepository(db_session).get_or_create(42)
    assert user.id is not None

    repo = PassportRepository(db_session)
    first = await repo.save_new(user.id, _passport(400))
    await repo.add_event(first.id, "question_asked", {"field": "budget.max"})
    second = await repo.save_revision(first, _passport(300))
    await repo.add_event(second.id, "user_message", {"field": "budget.max", "text": "до 300"})
    await db_session.commit()

    events = await repo.list_events(first.id)
    assert [(event.kind, event.payload.get("field")) for event in events] == [
        ("question_asked", "budget.max"),
        ("user_message", "budget.max"),
    ]


async def test_dialogue_state_is_read_back_after_a_restart(db_engine: AsyncEngine) -> None:
    """Начатый разговор переживает перезапуск процесса.

    Второй `PassportStore` со своей сессией — это и есть перезапуск бота:
    общего с первым у них только содержимое таблиц.
    """
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)

    def scope() -> AsyncSession:
        return sessions()

    client = Client(tg_user_id=42, username="dima")
    before = PassportStore(scope)
    dialogue = await before.load(client)
    dialogue = await before.start(dialogue, _passport(400))
    await before.note(dialogue, kind="question_asked", payload={"field": "budget.max"})
    assert dialogue.passport is not None

    resumed = await PassportStore(scope).load(client)

    assert resumed.passport is not None
    assert resumed.passport.id == dialogue.passport.id
    assert resumed.state.pending == "budget.max"
    assert resumed.state.asked == ("budget.max",)


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


# ── снимок наполнения для страницы «База» ───────────────────────────────────


async def test_registry_snapshot_shows_disabled_chats_too(db_session: AsyncSession) -> None:
    """`list_all` отвечает «что накоплено», а не «где искать сейчас».

    Выключенный чат из `list_active` исчезает по делу — искать в нём незачем.
    Но на странице наполнения его отсутствие читалось бы как «мы туда не
    вступали», то есть ровно наоборот.
    """
    repo = ChatRepository(db_session)
    await repo.add(Chat(tg_id=-100901, title="живой", city="nha_trang"))
    await repo.add(Chat(tg_id=-100902, title="выключенный", city="nha_trang", is_active=False))
    await db_session.commit()

    everything = {chat.tg_id for chat in await repo.list_all()}

    assert everything == {-100901, -100902}
    assert {chat.tg_id for chat in await repo.list_active()} == {-100901}


async def test_harvest_counts_are_one_query_per_page_not_per_chat(
    db_session: AsyncSession,
) -> None:
    """Сколько сырья принёс каждый чат — счётчик, по которому видно молчащий чат."""
    await RawMessageRepository(db_session).add_many(
        [_raw(1, -100901), _raw(2, -100901), _raw(3, -100902)]
    )
    await db_session.commit()

    counts = await RawMessageRepository(db_session).counts_by_chat()

    assert counts == {-100901: 2, -100902: 1}


async def test_recent_raw_comes_by_publication_not_by_insert(db_session: AsyncSession) -> None:
    """Догон истории приходит вразнобой: порядок вставки показал бы старое."""
    old = RawMessage(
        chat_tg_id=-100901,
        msg_id=10,
        text="старое",
        text_hash="hash-old",
        posted_at=NOW - timedelta(days=3),
    )
    fresh = RawMessage(
        chat_tg_id=-100901, msg_id=11, text="свежее", text_hash="hash-new", posted_at=NOW
    )
    # Вставляем старое ПОСЛЕ свежего — как и бывает при догоне.
    await RawMessageRepository(db_session).add_many([fresh])
    await RawMessageRepository(db_session).add_many([old])
    await db_session.commit()

    assert [message.text for message in await RawMessageRepository(db_session).recent()] == [
        "свежее",
        "старое",
    ]


async def test_queue_snapshot_keeps_the_order_reserve_uses(db_session: AsyncSession) -> None:
    """Порядок снимка обязан совпадать с порядком разбора.

    Иначе страница показывает «следующий — этот», а `reserve()` берёт другого,
    и застрявшего кандидата ищут не там, где он стоит.
    """
    repo = CandidateRepository(db_session)
    for key, priority in (("@third", 30), ("@first", 10), ("@second", 20)):
        await repo.push(DiscoveryCandidate(key=key, username=key.lstrip("@"), priority=priority))
    await db_session.commit()
    await repo.release("@first")  # неизвестный исход: попытка засчитана
    await db_session.commit()

    snapshot = await repo.snapshot()

    assert [item.key for item in snapshot] == ["@first", "@second", "@third"]
    assert snapshot[0].attempts == 1, "застрявший кандидат виден только по попыткам"
    assert await repo.counts_by_status() == {"queued": 3}


async def test_a_claimed_slot_without_an_outcome_stays_in_the_journal(
    db_session: AsyncSession,
) -> None:
    """Строка `claimed` без чата — потраченный впустую слот, и прятать её нельзя.

    Живой отказ 01.09.2026 выглядел именно так: два `claimed` без `tg_id`,
    реестр пуст. Если журнал показывает только удачные вступления, этот отказ
    на странице неотличим от «ещё не пробовали».
    """
    ledger = JoinLedgerRepository(db_session)
    event_id = await ledger.claim_slot(
        NOW, window=timedelta(hours=24), maximum=10, next_allowed_at=NOW + timedelta(hours=1)
    )
    assert event_id is not None
    await ledger.confirm_join(event_id=event_id, tg_id=-100903, username="joined_one")
    await ledger.claim_slot(
        NOW + timedelta(hours=2),
        window=timedelta(hours=24),
        maximum=10,
        next_allowed_at=NOW + timedelta(hours=3),
    )
    await db_session.commit()

    events = await ledger.recent_events()

    assert [event.kind for event in events] == ["claimed", "joined"]
    assert events[0].tg_id is None, "исхода нет — и это ровно то, что надо увидеть"


async def test_rejections_are_readable_with_their_reason(db_session: AsyncSession) -> None:
    repo = RejectRepository(db_session)
    await repo.reject("@danang001", "foreign_city")
    await db_session.commit()

    assert [(item.key, item.reason) for item in await repo.recent()] == [
        ("@danang001", "foreign_city")
    ]


# ── уборка сырья по сроку хранения ──────────────────────────────────────────


async def _age(session: AsyncSession, raw_id: int, *, days: int) -> None:
    """Состарить строку: `ingested_at` ставит база, вставкой его не задать."""
    await session.execute(
        update(models.RawMessage)
        .where(models.RawMessage.id == raw_id)
        .values(ingested_at=NOW - timedelta(days=days))
    )


async def test_retention_never_takes_a_listing_down_with_the_raw_message(
    db_session: AsyncSession,
) -> None:
    """Главная мина уборки: у `listings.raw_message_id` стоит ON DELETE CASCADE.

    Удаление сырья по возрасту унесло бы живую карточку, показанную клиенту,
    вместе с текстом, по которому verifier её сверяет. Тест на подделке этого
    не поймал бы: каскад существует только в настоящей схеме.
    """
    repo = RawMessageRepository(db_session)
    with_card, orphan = await repo.add_many([_raw(1), _raw(2)])
    await ListingRepository(db_session).add(
        Listing(
            raw_message_id=with_card,
            deal_type="sell",
            category="motorbike",
            city="nha_trang",
            title="Honda Vision 2021",
            summary="Автомат",
            tg_link="https://t.me/c/100123/1",
            posted_at=NOW,
        )
    )
    for raw_id in (with_card, orphan):
        await _age(db_session, raw_id, days=200)
    await db_session.commit()

    deleted = await repo.delete_expired(older_than=NOW - timedelta(days=90), limit=100)
    await db_session.commit()

    assert deleted == 1, "удалить полагалось ровно сироту"
    assert await repo.get_by_key(-100123, 1) is not None, "сырьё под карточкой обязано остаться"
    assert await repo.get_by_key(-100123, 2) is None
    assert await ListingRepository(db_session).get_by_raw_message(with_card) is not None


async def test_retention_counts_from_ingested_not_posted(db_session: AsyncSession) -> None:
    """Догон истории приносит посты двухлетней давности — и они не мусор.

    По `posted_at` уборка стирала бы архив ровно с той скоростью, с какой
    коллектор его дочитывает, и глубокий добор не имел бы смысла вовсе.
    """
    ancient = RawMessage(
        chat_tg_id=-100123,
        msg_id=42,
        text="Продам байк, объявление двухлетней давности",
        text_hash="hash-ancient",
        posted_at=NOW - timedelta(days=700),
    )
    repo = RawMessageRepository(db_session)
    await repo.add_many([ancient])
    await db_session.commit()

    deleted = await repo.delete_expired(older_than=NOW - timedelta(days=90), limit=100)
    await db_session.commit()

    assert deleted == 0, "скачано сегодня — значит хранится, каким бы старым ни был пост"
    assert await repo.get_by_key(-100123, 42) is not None


async def test_retention_deletes_in_batches(db_session: AsyncSession) -> None:
    """Первая уборка накопленного не должна быть одной длинной транзакцией."""
    repo = RawMessageRepository(db_session)
    ids = await repo.add_many([_raw(number) for number in range(1, 8)])
    for raw_id in ids:
        await _age(db_session, raw_id, days=120)
    await db_session.commit()

    assert await repo.delete_expired(older_than=NOW - timedelta(days=90), limit=3) == 3
    await db_session.commit()
    assert await repo.delete_expired(older_than=NOW - timedelta(days=90), limit=3) == 3
    await db_session.commit()
    assert await repo.delete_expired(older_than=NOW - timedelta(days=90), limit=3) == 1
    await db_session.commit()
    assert await repo.delete_expired(older_than=NOW - timedelta(days=90), limit=3) == 0


# ── курсор архива ───────────────────────────────────────────────────────────


async def test_the_archive_cursor_moves_down_not_up(db_session: AsyncSession) -> None:
    """Зеркало `mark_synced`, и сторона тут решает всё.

    `least` вместо `greatest`: перепутать значит либо застрять на месте, либо
    перескочить непрочитанное — второе тише и потому хуже. Ноль означает «ещё
    не начинали», поэтому в сравнение он попадать не должен вовсе.
    """
    repo = ChatRepository(db_session)
    await repo.add(Chat(tg_id=-100777, title="Барахолка", city="nha_trang"))
    await db_session.commit()

    await repo.mark_backfilled(-100777, oldest_msg_id=5000, done=False)
    await db_session.commit()
    first = await repo.get_by_tg_id(-100777)
    assert first is not None and first.backfill_msg_id == 5000, "нуль не должен выиграть у 5000"

    await repo.mark_backfilled(-100777, oldest_msg_id=4000, done=False)
    await db_session.commit()
    lower = await repo.get_by_tg_id(-100777)
    assert lower is not None and lower.backfill_msg_id == 4000

    await repo.mark_backfilled(-100777, oldest_msg_id=9000, done=False)
    await db_session.commit()
    back = await repo.get_by_tg_id(-100777)
    assert back is not None and back.backfill_msg_id == 4000, "курсор назад вверх не отпрыгивает"


async def test_a_finished_archive_is_never_offered_again(db_session: AsyncSession) -> None:
    repo = ChatRepository(db_session)
    await repo.add(Chat(tg_id=-100777, title="Дочитанная", city="nha_trang"))
    await repo.add(Chat(tg_id=-100888, title="Недочитанная", city="nha_trang"))
    await db_session.commit()
    await repo.mark_backfilled(-100777, oldest_msg_id=1, done=True)
    await db_session.commit()

    picked = await repo.next_backfill()

    assert picked is not None and picked.tg_id == -100888


async def test_an_untouched_chat_gets_the_archive_before_a_started_one(
    db_session: AsyncSession,
) -> None:
    """Чат без архива бесполезнее, чем чат с половиной архива."""
    repo = ChatRepository(db_session)
    await repo.add(Chat(tg_id=-100777, title="Начатая", city="nha_trang"))
    await repo.add(Chat(tg_id=-100888, title="Нетронутая", city="nha_trang"))
    await db_session.commit()
    await repo.mark_backfilled(-100777, oldest_msg_id=500, done=False)
    await db_session.commit()

    picked = await repo.next_backfill()

    assert picked is not None and picked.tg_id == -100888
    assert picked.backfill_msg_id == 0


async def test_a_disabled_chat_is_not_backfilled(db_session: AsyncSession) -> None:
    repo = ChatRepository(db_session)
    await repo.add(Chat(tg_id=-100777, title="Выключенная", city="nha_trang", is_active=False))
    await db_session.commit()

    assert await repo.next_backfill() is None


# ── дедуп кросспостов ───────────────────────────────────────────────────────


async def test_a_crosspost_is_recognised_by_its_fingerprint(db_session: AsyncSession) -> None:
    """Одно объявление в двух группах — две строки сырья и ОДНА карточка.

    Уникальность `(chat_tg_id, msg_id)` тут бесполезна: сообщения разные.
    Ловит только совпадение отпечатка, и проверять это надо на живой базе —
    вопрос в join между `listings` и `raw_messages`.
    """
    repo = RawMessageRepository(db_session)
    first, second = await repo.add_many(
        [
            RawMessage(
                chat_tg_id=-100111,
                msg_id=1,
                text="Продам Honda Vision 2021",
                text_hash="одинаковый-отпечаток",
                posted_at=NOW,
            ),
            RawMessage(
                chat_tg_id=-100222,
                msg_id=1,
                text="Продам Honda Vision 2021",
                text_hash="одинаковый-отпечаток",
                posted_at=NOW,
            ),
        ]
    )
    await db_session.commit()

    # Пока карточки нет ни у кого, дубликатом никто не считается.
    assert await repo.has_listing_for("одинаковый-отпечаток", besides=second) is False

    await ListingRepository(db_session).add(
        Listing(
            raw_message_id=first,
            deal_type="sell",
            category="motorbike",
            city="nha_trang",
            title="Honda Vision 2021",
            summary="Автомат",
            tg_link="https://t.me/c/100111/1",
            posted_at=NOW,
        )
    )
    await db_session.commit()

    assert await repo.has_listing_for("одинаковый-отпечаток", besides=second) is True
    assert await repo.has_listing_for("одинаковый-отпечаток", besides=first) is False, (
        "сообщение не может быть дубликатом самого себя"
    )
    assert await repo.has_listing_for("другой-отпечаток", besides=second) is False


async def test_the_stage_pass_can_refresh_a_stale_fingerprint(db_session: AsyncSession) -> None:
    """Схема отпечатка сменилась — старые строки чинятся тем же проходом воронки."""
    repo = RawMessageRepository(db_session)
    (raw_id,) = await repo.add_many([_raw(1)])
    await db_session.commit()

    await repo.set_stage([raw_id], "extracted", text_hash="свежий-отпечаток")
    await db_session.commit()

    stored = await repo.get_by_key(-100123, 1)
    assert stored is not None
    assert (stored.stage, stored.text_hash) == ("extracted", "свежий-отпечаток")


# ── подписки и доставка ─────────────────────────────────────────────────────


async def _subscriber(session: AsyncSession, passport: Passport) -> tuple[int, int]:
    """Клиент с подпиской на текущую версию паспорта. Возврат: (user_id, sub_id)."""
    user = await UserRepository(session).get_or_create(555, username="подписчик")
    assert user.id is not None
    stored = await PassportRepository(session).save_new(user.id, passport)
    await session.flush()
    row = models.Subscription(user_id=user.id, passport_root=stored.id)
    session.add(row)
    await session.flush()
    assert row.id is not None
    return user.id, row.id


async def test_a_subscription_follows_the_passport_it_was_made_for(
    db_session: AsyncSession,
) -> None:
    """Клиент правит запрос — подписка обязана следовать за правкой.

    Подписка хранит корень цепочки, а не версию. Проверять это надо на живой
    базе: всё держится на join по `COALESCE(root_id, id)` и частичном индексе
    `is_current`, а на подделке они не существуют.
    """
    passport = Passport(
        intent=Intent.BUY, category=Category.MOTORBIKE, city="nha_trang", raw_query="ищу скутер"
    )
    user_id, sub_id = await _subscriber(db_session, passport)
    await db_session.commit()

    repo = PassportRepository(db_session)
    current = await repo.get_current(user_id)
    assert current is not None
    revised = passport.model_copy(update={"budget": Budget(max=400, currency=Currency.USD)})
    await repo.save_revision(current, revised)
    await db_session.commit()

    live = await DeliveryRepository(db_session).active_subscriptions()

    assert [item.id for item in live] == [sub_id]
    assert live[0].passport.passport.budget.max == 400, "подписка застыла на старой версии"


async def test_the_same_listing_is_never_queued_twice(db_session: AsyncSession) -> None:
    """Воркер идёт пачками и встретит карточку снова — дважды слать нельзя."""
    passport = Passport(intent=Intent.BUY, category=Category.MOTORBIKE, city="nha_trang")
    user_id, sub_id = await _subscriber(db_session, passport)
    (raw_id,) = await RawMessageRepository(db_session).add_many([_raw(1)])
    card = await ListingRepository(db_session).add(
        Listing(
            raw_message_id=raw_id,
            deal_type="sell",
            category="motorbike",
            city="nha_trang",
            title="Honda Vision",
            summary="Автомат",
            tg_link="https://t.me/c/1/1",
            posted_at=NOW,
        )
    )
    await db_session.commit()
    assert card.id is not None

    repo = DeliveryRepository(db_session)
    first = await repo.enqueue(
        subscription_id=sub_id, user_id=user_id, listing_id=card.id, score=0.9, payload={"a": 1}
    )
    second = await repo.enqueue(
        subscription_id=sub_id, user_id=user_id, listing_id=card.id, score=0.9, payload={"a": 1}
    )
    await db_session.commit()

    assert (first, second) == (True, False)
    assert len(await repo.take_pending()) == 1, "в очереди обязана быть одна строка"
    assert await repo.sent_since(sub_id, since=NOW) == 0
    assert await repo.used_since(sub_id, since=NOW) == 1
    (pending,) = await repo.take_pending()
    await repo.mark_sent(pending.id, now=NOW + timedelta(minutes=1))
    await db_session.commit()
    assert await repo.sent_since(sub_id, since=NOW) == 1


async def test_a_message_scheduled_for_later_is_not_taken_now(db_session: AsyncSession) -> None:
    """Повтор после неудачи ждёт своего часа, а не крутится в цикле."""
    passport = Passport(intent=Intent.BUY, category=Category.MOTORBIKE, city="nha_trang")
    user_id, _ = await _subscriber(db_session, passport)
    # Время ставим явно: у колонок стоит server_default now(), и тест, который
    # на него полагается, проверяет не запрос, а показания часов машины.
    db_session.add(models.Outbox(user_id=user_id, payload={"a": 1}, scheduled_at=NOW))
    await db_session.commit()

    repo = DeliveryRepository(db_session)
    (message,) = await repo.take_pending(now=NOW)
    await repo.mark_failed(message.id, retry_at=NOW + timedelta(minutes=15))
    await db_session.commit()

    assert await repo.take_pending(now=NOW) == []
    assert len(await repo.take_pending(now=NOW + timedelta(minutes=16))) == 1


async def test_a_new_listing_reaches_the_subscriber_queue(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Замыкающее звено контура: карточка → подписка → очередь доставки.

    Проверяется на живой базе целиком, потому что весь смысл здесь в join между
    подпиской, паспортом и карточками — на подделке он зелёный при любой
    ошибке. До этой ветки звена не существовало: `listings` копились, а
    `outbox` не наполнял никто.
    """
    from sniffer.worker import matcher as module

    passport = Passport(
        intent=Intent.BUY, category=Category.MOTORBIKE, city="nha_trang", raw_query="ищу скутер"
    )
    user_id, sub_id = await _subscriber(db_session, passport)
    (raw_id,) = await RawMessageRepository(db_session).add_many([_raw(1)])
    await ListingRepository(db_session).add(
        Listing(
            raw_message_id=raw_id,
            deal_type="sell",
            category="motorbike",
            city="nha_trang",
            title="Honda Vision 2021",
            summary="Автомат, документы есть",
            tg_link="https://t.me/c/1/1",
            posted_at=NOW,
        )
    )
    await db_session.commit()

    monkeypatch.setattr(module, "session_scope", lambda: _borrowed(db_session))
    queued = await module.Matcher().tick(now=NOW)

    assert queued == 1
    repo = DeliveryRepository(db_session)
    (message,) = await repo.take_pending(now=NOW)
    assert message.user_id == user_id
    assert message.payload["title"] == "Honda Vision 2021"
    assert await repo.sent_since(sub_id, since=NOW.replace(hour=0)) == 0
    assert await repo.used_since(sub_id, since=NOW.replace(hour=0)) == 1

    # Второй проход не шлёт то же самое второй раз.
    assert await module.Matcher().tick(now=NOW) == 0


async def test_a_listing_from_another_city_is_not_sent(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Дананговская карточка нячангскому подписчику — это спам, а не находка."""
    from sniffer.worker import matcher as module

    passport = Passport(intent=Intent.BUY, category=Category.MOTORBIKE, city="nha_trang")
    await _subscriber(db_session, passport)
    (raw_id,) = await RawMessageRepository(db_session).add_many([_raw(1)])
    await ListingRepository(db_session).add(
        Listing(
            raw_message_id=raw_id,
            deal_type="sell",
            category="motorbike",
            city="da_nang",
            title="Honda Vision",
            summary="Автомат",
            tg_link="https://t.me/c/1/1",
            posted_at=NOW,
        )
    )
    await db_session.commit()

    monkeypatch.setattr(module, "session_scope", lambda: _borrowed(db_session))

    assert await module.Matcher().tick(now=NOW) == 0


async def test_matcher_advances_past_a_rejected_page(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Неподходящая первая страница не должна навсегда закрывать следующую."""
    from sniffer.worker import matcher as module

    passport = Passport(
        intent=Intent.BUY,
        category=Category.MOTORBIKE,
        city="nha_trang",
        attributes={"brand": "honda"},
    )
    _user_id, sub_id = await _subscriber(db_session, passport)
    await db_session.execute(
        update(models.Subscription).where(models.Subscription.id == sub_id).values(max_per_day=100)
    )
    raw_ids = await RawMessageRepository(db_session).add_many([_raw(1), _raw(2)])
    for raw_id, brand in zip(raw_ids, ("yamaha", "honda"), strict=True):
        await ListingRepository(db_session).add(
            Listing(
                raw_message_id=raw_id,
                deal_type="sell",
                category="motorbike",
                city="nha_trang",
                title=f"{brand} bike",
                summary="fresh",
                tg_link=f"https://t.me/c/1/{raw_id}",
                attributes={"brand": brand},
                posted_at=NOW,
            )
        )
    await db_session.commit()

    monkeypatch.setattr(module, "session_scope", lambda: _borrowed(db_session))
    monkeypatch.setattr(module, "LISTINGS_PER_SUBSCRIPTION", 1)

    assert await module.Matcher().tick(now=NOW) == 0
    state = (await DeliveryRepository(db_session).active_subscriptions(now=NOW))[0]
    assert state.scan_listing_id > state.since_listing_id
    await db_session.commit()
    assert await module.Matcher().tick(now=NOW) == 1


async def test_live_listing_is_idempotent_and_searchable_from_the_catalog(
    db_session: AsyncSession,
) -> None:
    listing = Listing(
        raw_message_id=None,
        source="chotot",
        external_id="external-42",
        deal_type="sell",
        category="motorbike",
        city="nha_trang",
        title="Honda Lead",
        summary="verified live result",
        tg_link="https://example.test/42",
        posted_at=NOW,
    )
    repo = ListingRepository(db_session)
    assert await repo.upsert_external(listing)
    assert not await repo.upsert_external(listing)
    await db_session.commit()

    from sniffer.domain.records import MatchFilter

    rows = await repo.search_catalog(
        MatchFilter(city="nha_trang", category="motorbike", deal_type="sell")
    )
    assert [(row.source, row.external_id) for row in rows] == [("chotot", "external-42")]


@asynccontextmanager
async def _borrowed(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Сессия теста вместо своей: проверяем запросы, а не сборку соединения."""
    yield session


async def test_one_broken_message_does_not_stop_the_whole_batch(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Единая точка отказа с чужим текстом на входе — это не надёжность.

    Живой отказ 01.09.2026: одно объявление с невставляемой ценой уронило пачку,
    воркер ушёл в цикл перезапуска, и вся воронка встала навсегда. Проверять
    надо на живой базе: падает именно вставка, и на подделке её нет.
    """
    from sniffer.pipeline import archive as pipeline
    from sniffer.worker import archive as module

    chat = Chat(tg_id=-100123, title="Барахолка", city="nha_trang", username="flea")
    await ChatRepository(db_session).add(chat)
    good, bad = await RawMessageRepository(db_session).add_many(
        [
            RawMessage(
                chat_tg_id=-100123,
                msg_id=1,
                text="Продам Honda Vision 2021, цена 15.000.000 VND, документы есть",
                text_hash="хороший",
                posted_at=NOW,
            ),
            RawMessage(
                chat_tg_id=-100123,
                msg_id=2,
                text="Продам Yamaha NVX, цена 20.000.000 VND, срочно",
                text_hash="сломанный",
                posted_at=NOW,
            ),
        ]
    )
    await db_session.commit()

    # Ломаем ровно одно сообщение — так, как это сделала неправдоподобная цена.
    real_listing = pipeline.listing_from

    def explode(raw: RawMessage, chat_row: Chat, result: GateResult) -> Listing:
        if raw.id == bad:
            raise ValueError("цена не влезла в колонку")
        return real_listing(raw, chat_row, result)

    monkeypatch.setattr(module, "listing_from", explode)
    monkeypatch.setattr(module, "session_scope", lambda: _borrowed(db_session))

    handled = await module.ArchivePipeline().tick()

    assert handled == 2, "оба сообщения обязаны быть разобраны, а не одно"
    listings = ListingRepository(db_session)
    assert await listings.get_by_raw_message(good) is not None, "здоровое стало карточкой"
    assert await listings.get_by_raw_message(bad) is None
    broken = await RawMessageRepository(db_session).get_by_key(-100123, 2)
    assert broken is not None and broken.stage == "rejected"
    assert broken.gate_signals.get("reason") == "pipeline_error"


# ── деньги: подписка за звёзды ──────────────────────────────────────────────


async def test_the_same_payment_never_extends_a_subscription_twice(
    db_session: AsyncSession,
) -> None:
    """Идемпотентность платежа. Telegram ПОВТОРЯЕТ апдейт, если бот не ответил.

    Проверять надо на живой базе: держится всё на `payments.external_id UNIQUE`
    и `ON CONFLICT DO NOTHING`, а на подделке ни того, ни другого нет. Деньги
    нельзя обработать «примерно один раз».
    """
    passport = Passport(intent=Intent.BUY, category=Category.MOTORBIKE, city="nha_trang")
    user = await UserRepository(db_session).get_or_create(777, username="платящий")
    assert user.id is not None
    stored = await PassportRepository(db_session).save_new(user.id, passport)
    await db_session.commit()

    repo = DeliveryRepository(db_session)
    payment = Payment(user_id=user.id, amount=1, external_id="charge-повтор")
    until = NOW + timedelta(days=30)

    first = await repo.pay_and_activate(
        payment, passport_root=stored.id, until=until, since_listing_id=100
    )
    await db_session.commit()
    second = await repo.pay_and_activate(
        payment, passport_root=stored.id, until=until + timedelta(days=30), since_listing_id=999
    )
    await db_session.commit()

    assert (first, second) == (True, False), "повторный апдейт не должен продлевать"
    state = await repo.subscription_for(user_id=user.id, passport_root=stored.id)
    assert state is not None
    assert state.expires_at == until, "срок остался от первого платежа"
    assert state.since_listing_id == 100, "точка отсчёта не сдвинулась"


async def test_a_renewal_extends_the_term_but_keeps_the_starting_point(
    db_session: AsyncSession,
) -> None:
    """Продление сдвигает срок и НЕ трогает точку отсчёта.

    Иначе клиент терял бы всё, что накопилось за оплаченный месяц: подписка
    начинала бы считать «новое» заново с момента списания.
    """
    passport = Passport(intent=Intent.BUY, category=Category.MOTORBIKE, city="nha_trang")
    user = await UserRepository(db_session).get_or_create(778)
    assert user.id is not None
    stored = await PassportRepository(db_session).save_new(user.id, passport)
    await db_session.commit()

    repo = DeliveryRepository(db_session)
    await repo.pay_and_activate(
        Payment(user_id=user.id, amount=1, external_id="месяц-1"),
        passport_root=stored.id,
        until=NOW + timedelta(days=30),
        since_listing_id=50,
    )
    await db_session.commit()
    await repo.pay_and_activate(
        Payment(user_id=user.id, amount=1, external_id="месяц-2", is_recurring=True),
        passport_root=stored.id,
        until=NOW + timedelta(days=60),
        since_listing_id=900,
    )
    await db_session.commit()

    state = await repo.subscription_for(user_id=user.id, passport_root=stored.id)
    assert state is not None
    assert state.expires_at == NOW + timedelta(days=60)
    assert state.since_listing_id == 50, "продление не начинает слежение заново"


async def test_an_expired_subscription_stops_receiving_cards(db_session: AsyncSession) -> None:
    """Кончились деньги — кончилась рассылка, и без всякого сторожа.

    Срок проверяется прямо в запросе активных подписок: пропущенный проход
    отдельного сторожа означал бы бесплатную рассылку, а пропущенное условие в
    запросе не означает ничего — его просто нет.
    """
    passport = Passport(intent=Intent.BUY, category=Category.MOTORBIKE, city="nha_trang")
    user = await UserRepository(db_session).get_or_create(779)
    assert user.id is not None
    stored = await PassportRepository(db_session).save_new(user.id, passport)
    await db_session.commit()

    repo = DeliveryRepository(db_session)
    await repo.pay_and_activate(
        Payment(user_id=user.id, amount=1, external_id="истёкший"),
        passport_root=stored.id,
        until=NOW,
        since_listing_id=0,
    )
    await db_session.commit()

    assert await repo.active_subscriptions(now=NOW - timedelta(days=1)) != []
    assert await repo.active_subscriptions(now=NOW + timedelta(seconds=1)) == []


async def test_a_forged_payload_cannot_subscribe_to_someone_elses_request(
    db_session: AsyncSession,
) -> None:
    """`payload` формируем мы, но приходит он от Telegram и доверенным не является."""
    passport = Passport(intent=Intent.BUY, category=Category.MOTORBIKE, city="nha_trang")
    mine = await UserRepository(db_session).get_or_create(780)
    stranger = await UserRepository(db_session).get_or_create(781)
    assert mine.id is not None and stranger.id is not None
    stored = await PassportRepository(db_session).save_new(mine.id, passport)
    await db_session.commit()

    repo = DeliveryRepository(db_session)

    assert await repo.owns_chain(user_id=mine.id, passport_root=stored.id) is True
    assert await repo.owns_chain(user_id=stranger.id, passport_root=stored.id) is False
    assert await repo.owns_chain(user_id=mine.id, passport_root=stored.id + 999) is False


async def test_a_subscription_only_gets_listings_newer_than_itself(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Подписка обещает НОВЫЕ посты, а не пересказ выдачи, из которой не выбрали.

    Без точки отсчёта свежая подписка вываливает клиенту весь двухнедельный
    запас разом — включая ровно те объявления, за отсутствие интереса к которым
    он и заплатил.
    """
    from sniffer.worker import matcher as module

    passport = Passport(intent=Intent.BUY, category=Category.MOTORBIKE, city="nha_trang")
    user = await UserRepository(db_session).get_or_create(782)
    assert user.id is not None
    stored = await PassportRepository(db_session).save_new(user.id, passport)
    old_raw, new_raw = await RawMessageRepository(db_session).add_many([_raw(1), _raw(2)])
    listings = ListingRepository(db_session)
    seen = await listings.add(_card(old_raw, "Показывали до подписки"))
    await db_session.commit()
    assert seen.id is not None

    await DeliveryRepository(db_session).pay_and_activate(
        Payment(user_id=user.id, amount=1, external_id="за-новое"),
        passport_root=stored.id,
        until=NOW + timedelta(days=30),
        since_listing_id=seen.id,
    )
    fresh = await listings.add(_card(new_raw, "Появилось после подписки"))
    await db_session.commit()
    assert fresh.id is not None

    monkeypatch.setattr(module, "session_scope", lambda: _borrowed(db_session))
    assert await module.Matcher().tick(now=NOW) == 1

    (message,) = await DeliveryRepository(db_session).take_pending(now=NOW)
    assert message.payload["title"] == "Появилось после подписки"


def _card(raw_message_id: int, title: str) -> Listing:
    return Listing(
        raw_message_id=raw_message_id,
        deal_type="sell",
        category="motorbike",
        city="nha_trang",
        title=title,
        summary="Автомат",
        tg_link="https://t.me/c/1/1",
        posted_at=NOW,
    )
