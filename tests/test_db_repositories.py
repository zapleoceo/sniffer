"""Репозитории на живом Postgres.

Пропускаются, пока не задан `TEST_DATABASE_URL` (см. `conftest.py`): проверять
`ON CONFLICT DO NOTHING`, `FOR UPDATE SKIP LOCKED` и `TEXT[]` не на Postgres
бессмысленно — именно эти места и ломаются, а на подделке они зелёные всегда.
"""

from __future__ import annotations

import asyncio
import os
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
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.domain.records import Chat, DiscoveryCandidate, Listing, RawMessage

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
