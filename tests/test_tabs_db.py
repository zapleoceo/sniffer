"""Вкладки на живом Postgres и в хранилище диалога (в CI; локально пропускается)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sniffer.bot.store import Client, PassportStore
from sniffer.db import models
from sniffer.db.repositories import PassportRepository, UserRepository
from sniffer.db.repositories.tabs import TabRepository
from sniffer.db.repositories.watch import WatchRepository
from sniffer.domain.passport import Category, Intent, Passport

pytestmark = pytest.mark.usefixtures("db_engine")


def bike() -> Passport:
    return Passport(
        intent=Intent.BUY, category=Category.MOTORBIKE, city="nha_trang", raw_query="скутер"
    )


async def new_user(session: AsyncSession, tg: int) -> int:
    user = await UserRepository(session).get_or_create(tg, username=None)
    assert user.id is not None
    await session.commit()
    return user.id


async def test_a_claimed_thread_resolves_to_its_root_and_only_while_open(
    db_session: AsyncSession,
) -> None:
    user_id = await new_user(db_session, 1)
    root = (await PassportRepository(db_session).save_new(user_id, bike())).root
    tabs = TabRepository(db_session)

    assert await tabs.claim(user_id, root, 700, "Мотобайк")
    assert await tabs.root_of(user_id, 700) == root
    assert await tabs.root_of(user_id, 701) is None
    assert await tabs.has_topics(user_id)


async def test_a_thread_cannot_be_claimed_twice_and_a_root_cannot_have_two_threads(
    db_session: AsyncSession,
) -> None:
    user_id = await new_user(db_session, 2)
    passports = PassportRepository(db_session)
    a = (await passports.save_new(user_id, bike())).root
    b = (await passports.save_new(user_id, bike())).root
    tabs = TabRepository(db_session)

    assert await tabs.claim(user_id, a, 700)
    assert not await tabs.claim(user_id, b, 700), "тема уже занята другим поиском"
    assert not await tabs.claim(user_id, a, 701), "у поиска уже есть тема"


async def test_the_same_thread_id_may_belong_to_two_different_people(
    db_session: AsyncSession,
) -> None:
    one, two = await new_user(db_session, 3), await new_user(db_session, 4)
    passports = PassportRepository(db_session)
    a = (await passports.save_new(one, bike())).root
    b = (await passports.save_new(two, bike())).root
    tabs = TabRepository(db_session)
    assert await tabs.claim(one, a, 700) and await tabs.claim(two, b, 700)
    assert await tabs.root_of(two, 700) == b


async def test_an_archived_search_never_gets_a_topic_back(db_session: AsyncSession) -> None:
    user_id = await new_user(db_session, 5)
    root = (await PassportRepository(db_session).save_new(user_id, bike())).root
    await WatchRepository(db_session).archive(user_id, root)
    tabs = TabRepository(db_session)

    assert not await tabs.attach(user_id, root, 800, "x")
    assert await tabs.root_of(user_id, 800) is None


async def test_a_lost_link_is_recreated_by_attach_and_serves_the_new_thread(
    db_session: AsyncSession,
) -> None:
    user_id = await new_user(db_session, 6)
    root = (await PassportRepository(db_session).save_new(user_id, bike())).root
    db_session.add(
        models.Subscription(
            user_id=user_id,
            passport_root=root,
            expires_at=datetime.now(UTC) + timedelta(days=3),
        )
    )
    await db_session.commit()
    tabs = TabRepository(db_session)
    await tabs.claim(user_id, root, 700)
    sub = await db_session.scalar(select(models.Subscription.id))
    assert sub is not None
    assert await tabs.threads_for([sub]) == {sub: 700}

    assert await tabs.mark_lost(sub)
    assert await tabs.threads_for([sub]) == {}
    assert await tabs.root_of(user_id, 700) is None

    assert await tabs.attach(user_id, root, 701, "Мотобайк")
    assert await tabs.threads_for([sub]) == {sub: 701}


async def test_threads_for_of_nothing_is_empty_without_a_query(db_session: AsyncSession) -> None:
    assert await TabRepository(db_session).threads_for([]) == {}


async def test_the_database_itself_refuses_a_state_outside_the_closed_list(
    db_session: AsyncSession,
) -> None:
    user_id = await new_user(db_session, 7)
    db_session.add(models.SearchTab(user_id=user_id, passport_root=1, state="whatever"))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_a_message_in_a_known_thread_works_on_its_search_not_on_the_pointer(
    db_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    store = PassportStore(lambda: sessions())
    first = await store.load(Client(10))
    a = await store.start(first, bike())
    second = await store.load(Client(10))
    b = await store.start(second, bike().model_copy(update={"city": "da_nang"}))
    async with sessions() as session:
        assert a.passport is not None and b.passport is not None
        await TabRepository(session).claim(a.user_id, a.passport.root, 700)
        await TabRepository(session).claim(a.user_id, b.passport.root, 701)
        await session.commit()

    in_a = await store.load(Client(10, thread_id=700))
    in_b = await store.load(Client(10, thread_id=701))

    # Указатель клиента сейчас на `b`, но тема 700 обязана вести `a`.
    assert in_a.passport is not None and in_a.passport.root == a.passport.root
    assert in_b.passport is not None and in_b.passport.root == b.passport.root
    assert in_a.thread_id == 700


async def test_select_in_topic_keeps_general_pointer_and_returns_selected_root(
    db_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    store = PassportStore(lambda: sessions())
    general = await store.start(await store.load(Client(101)), bike())
    topic = await store.start(
        await store.load(Client(101, thread_id=702)),
        bike().model_copy(update={"city": "da_nang"}),
    )
    assert general.passport is not None and topic.passport is not None

    selected = await store.select(await store.load(Client(101, thread_id=702)), topic.passport.root)

    assert selected.passport is not None and selected.passport.root == topic.passport.root
    assert (await store.load(Client(101))).passport.root == general.passport.root  # type: ignore[union-attr]


async def test_select_in_topic_rejects_another_users_root(db_engine: AsyncEngine) -> None:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    store = PassportStore(lambda: sessions())
    owner = await store.start(await store.load(Client(102)), bike())
    topic = await store.start(await store.load(Client(103, thread_id=703)), bike())
    assert owner.passport is not None and topic.passport is not None

    selected = await store.select(await store.load(Client(103, thread_id=703)), owner.passport.root)

    assert selected.passport.root == topic.passport.root  # type: ignore[union-attr]


async def test_select_in_topic_rejects_another_owned_topic_root(db_engine: AsyncEngine) -> None:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    store = PassportStore(lambda: sessions())
    a = await store.start(await store.load(Client(104, thread_id=704)), bike())
    b = await store.start(await store.load(Client(104, thread_id=705)), bike())
    assert a.passport is not None and b.passport is not None

    selected = await store.select(await store.load(Client(104, thread_id=705)), a.passport.root)

    assert selected.passport is not None and selected.passport.root == b.passport.root


async def test_a_new_topic_starts_a_search_and_links_itself(db_engine: AsyncEngine) -> None:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    store = PassportStore(lambda: sessions())
    empty = await store.load(Client(11, thread_id=900))
    assert empty.passport is None and empty.thread_id == 900

    opened = await store.start(empty, bike())

    assert opened.thread_id == 900
    again = await store.load(Client(11, thread_id=900))
    assert again.passport is not None and again.passport.root == opened.passport.root  # type: ignore[union-attr]


async def test_the_loser_of_a_race_in_a_new_topic_joins_the_winner_and_leaves_no_second_search(
    db_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    store = PassportStore(lambda: sessions())
    empty = await store.load(Client(12, thread_id=901))

    winner = await store.start(empty, bike())
    loser = await store.start(empty, bike().model_copy(update={"city": "da_nang"}))

    assert loser.passport is not None and winner.passport is not None
    assert loser.passport.root == winner.passport.root
    async with sessions() as session:
        user_id = winner.user_id
        versions = await PassportRepository(session).list_queries(user_id, limit=20)
        assert len(versions) == 1, "проигравший не оставил вторую ветку"


async def test_a_message_without_a_thread_still_uses_the_old_pointer_path(
    db_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    store = PassportStore(lambda: sessions())
    started = await store.start(await store.load(Client(13)), bike())
    loaded = await store.load(Client(13))
    assert loaded.thread_id is None
    assert loaded.passport is not None and loaded.passport.root == started.passport.root  # type: ignore[union-attr]
