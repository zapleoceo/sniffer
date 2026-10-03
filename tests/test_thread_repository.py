"""Поиски на живой базе: управление по корню, порядок по использованию, гонка флага.

Пропускаются без `TEST_DATABASE_URL` (см. `conftest.py`): то, что здесь проверяется, —
свойства самого Postgres (блокировка строки, `RETURNING`, `LIMIT`, порядок), и подделка
проверяла бы подделку. Форму тех же запросов без базы держит `test_store_linkage.py`.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sniffer.bot.store import Client, PassportStore
from sniffer.bot.threads import open_thread
from sniffer.db import collection_models as _collection_models  # noqa: F401
from sniffer.db import models
from sniffer.db.repositories import PassportRepository, UserRepository
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.domain.threads import MAX_LIVE_THREADS
from tests.subscription_support import grant

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL не задан: живого Postgres нет",
)


def passport(number: int) -> Passport:
    return Passport(
        intent=Intent.BUY,
        category=Category.MOTORBIKE,
        city="nha_trang",
        budget=Budget(max=100 + number, currency=Currency.USD),
        raw_query=f"ищу скутер {number}",
    )


async def _user(session: AsyncSession, tg_id: int) -> int:
    user = await UserRepository(session).get_or_create(tg_id)
    assert user.id is not None
    return user.id


# ── управление по принадлежности: get_query ────────────────────────────────


async def test_a_pushed_out_search_is_still_found_by_its_root(db_session: AsyncSession) -> None:
    """`get_query` не знает о пределе списка: вытесненный поиск остаётся поиском клиента."""
    user_id = await _user(db_session, 51)
    repo = PassportRepository(db_session)
    chains = [await repo.save_new(user_id, passport(step)) for step in range(MAX_LIVE_THREADS + 1)]
    await db_session.commit()
    oldest = chains[0].root
    assert oldest not in {row.root for row in await repo.list_queries(user_id)}

    found = await repo.get_query(user_id, oldest)

    assert found is not None
    assert found.root == oldest
    assert found.passport.raw_query == "ищу скутер 0"


async def test_a_foreign_or_unknown_root_is_not_found(db_session: AsyncSession) -> None:
    mine = await _user(db_session, 52)
    stranger = await _user(db_session, 53)
    repo = PassportRepository(db_session)
    theirs = await repo.save_new(stranger, passport(1))
    await db_session.commit()

    assert await repo.get_query(mine, theirs.root) is None, "чужой корень"
    assert await repo.get_query(mine, 987_654) is None, "несуществующий корень"


async def test_one_search_answers_like_its_row_in_the_list(db_session: AsyncSession) -> None:
    """Список и одиночный запрос описывают поиск одинаково: один запрос, а не два."""
    user_id = await _user(db_session, 54)
    repo = PassportRepository(db_session)
    chain = await repo.save_new(user_id, passport(1))
    await db_session.commit()

    (listed,) = await repo.list_queries(user_id)
    single = await repo.get_query(user_id, chain.root)

    assert single == listed


@pytest.mark.parametrize(
    ("expires", "paused", "expected"),
    [
        (timedelta(days=30), False, "active"),
        (timedelta(days=30), True, "paused"),
        (-timedelta(days=1), False, "expired"),
    ],
    ids=["active", "paused", "expired"],
)
async def test_monitoring_state_comes_from_the_subscription(
    db_session: AsyncSession, expires: timedelta, paused: bool, expected: str
) -> None:
    """Сообщение о вытеснении и карточка говорят о мониторинге по подписке, а не по умолчанию."""
    user_id = await _user(db_session, 55)
    repo = PassportRepository(db_session)
    chain = await repo.save_new(user_id, passport(1))
    await grant(db_session, user_id, chain.root, until=datetime.now(UTC) + expires)
    if paused:
        assert await DeliveryRepository(db_session).set_active(
            user_id=user_id, passport_root=chain.root, active=False
        )
    await db_session.commit()

    found = await repo.get_query(user_id, chain.root)
    (listed,) = await repo.list_queries(user_id)

    assert found is not None and found.monitoring == expected
    assert listed.monitoring == expected


async def test_without_a_subscription_the_search_has_no_monitoring(
    db_session: AsyncSession,
) -> None:
    user_id = await _user(db_session, 56)
    repo = PassportRepository(db_session)
    chain = await repo.save_new(user_id, passport(1))
    await db_session.commit()

    found = await repo.get_query(user_id, chain.root)

    assert found is not None and found.monitoring == "off"


async def test_the_notice_follows_the_real_subscription_on_a_real_database(
    db_engine: AsyncEngine,
) -> None:
    """Уведомление о вытеснении по настоящей подписке: сборка от базы до текста целиком.

    Подделка хранилища знает о мониторинге только то, что ей сказали; здесь вытесняемый
    поиск оплачен в базе, и текст обязан это назвать.
    """
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    store = PassportStore(lambda: sessions())
    client = Client(tg_user_id=61, username="dima")
    dialogue = await store.load(client)
    first_root = 0
    for number in range(1, MAX_LIVE_THREADS + 1):
        dialogue = await store.start(dialogue, passport(number))
        assert dialogue.passport is not None
        first_root = first_root or dialogue.passport.root
    async with sessions() as session:
        await grant(
            session, dialogue.user_id, first_root, until=datetime.now(UTC) + timedelta(days=30)
        )
        await session.commit()

    opened = await open_thread(store, dialogue, passport(99))

    assert opened is not None and opened.notice is not None
    assert "мониторинг продолжает работать" in opened.notice
    assert "ищу скутер" not in opened.notice, "название, а не формулировка"


# ── порядок списка: по использованию ───────────────────────────────────────


async def test_a_choice_brings_a_pushed_out_search_back_into_the_list(
    db_session: AsyncSession,
) -> None:
    """Выбор поднимает поиск: он снова в списке, наверху, и «✓» стоит на нём."""
    user_id = await _user(db_session, 57)
    repo = PassportRepository(db_session)
    chains = [await repo.save_new(user_id, passport(step)) for step in range(MAX_LIVE_THREADS + 1)]
    await db_session.commit()
    oldest, second = chains[0].root, chains[1].root
    assert oldest not in {row.root for row in await repo.list_queries(user_id)}

    assert await repo.select(user_id, oldest)
    await db_session.commit()

    live = await repo.list_queries(user_id)
    assert live[0].root == oldest
    assert live[0].is_active
    assert len(live) == MAX_LIVE_THREADS
    assert second not in {row.root for row in live}, "вытеснен следующий по давности"


async def test_a_search_without_a_use_stamp_is_ordered_by_its_creation(
    db_session: AsyncSession,
) -> None:
    """Поиски, которых не трогали с появления колонки, идут в прежнем порядке — по `created_at`.

    Время создания первого сдвинуто вперёд намеренно: порядок по `id` дал бы обратное, и
    тест отличает «по созданию» от «по номеру».
    """
    user_id = await _user(db_session, 58)
    repo = PassportRepository(db_session)
    first = await repo.save_new(user_id, passport(1))
    second = await repo.save_new(user_id, passport(2))
    await db_session.execute(
        update(models.Passport)
        .where(models.Passport.id == first.id)
        .values(created_at=datetime.now(UTC) + timedelta(hours=1))
    )
    await db_session.execute(update(models.Passport).values(last_used_at=None))
    await db_session.commit()

    assert [row.root for row in await repo.list_queries(user_id)] == [first.root, second.root]


# ── флаг `/new`: одно взведение — один поиск, и на настоящей базе ──────────


async def test_two_sessions_cannot_both_spend_the_same_flag(db_engine: AsyncEngine) -> None:
    """Условный `UPDATE … RETURNING` отдаёт строку ровно одному: второй ждёт блокировку.

    Транзакция победителя держится открытой дольше проигравшего, чтобы вторая сессия
    дошла до `UPDATE` ПОКА первая не закоммитила, — иначе гонки не было бы вовсе.
    """
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as setup:
        user_id = await _user(setup, 59)
        await PassportRepository(setup).await_new_request(user_id)
        await setup.commit()

    async def spend() -> bool:
        async with sessions() as session:
            won = await PassportRepository(session).consume_new_request(user_id)
            await asyncio.sleep(0.2)
            await session.commit()
            return won

    outcomes = await asyncio.gather(spend(), spend())

    assert sorted(outcomes) == [False, True]


async def test_two_stores_open_one_search_for_one_new(db_engine: AsyncEngine) -> None:
    """То же на уровне хранилища: два сообщения со старым снимком — одна новая ветка.

    Проигравший не вставляет ничего, и в базе после гонки — прежний поиск и ровно один новый.
    """
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    client = Client(tg_user_id=60, username="dima")
    first_store = PassportStore(lambda: sessions())
    dialogue = await first_store.load(client)
    dialogue = await first_store.start(dialogue, passport(1))
    await first_store.await_new(dialogue)
    stale = await first_store.load(client)
    assert stale.starting_new

    results = await asyncio.gather(
        PassportStore(lambda: sessions()).start_requested(stale, passport(2)),
        PassportStore(lambda: sessions()).start_requested(stale, passport(3)),
    )

    assert sum(result is not None for result in results) == 1
    after = await first_store.load(client)
    assert after.starting_new is False
    assert len(await first_store.live_threads(after)) == 2, "прежний и один новый"
