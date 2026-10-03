"""Правка фильтра, слоты и архив поиска на живом Postgres (в CI; локально пропускается)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db import models
from sniffer.db.repositories import PassportRepository, UserRepository
from sniffer.db.repositories.passport_edit import PassportEditor, StaleVersion
from sniffer.db.repositories.watch import WatchRepository
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.domain.passport_edit import Change, EditError

pytestmark = pytest.mark.usefixtures("db_engine")


def bike(city: str = "nha_trang") -> Passport:
    return Passport(
        intent=Intent.BUY,
        category=Category.MOTORBIKE,
        city=city,
        budget=Budget(max=15_000_000, currency=Currency.VND),
        attributes={"brand": "honda", "transmission": "automatic"},
        raw_query="скутер",
    )


async def user_with(session: AsyncSession, tg: int, searches: int = 1) -> tuple[int, list[int]]:
    user = await UserRepository(session).get_or_create(tg, username=None)
    assert user.id is not None
    roots = [
        (await PassportRepository(session).save_new(user.id, bike(f"city{i}"))).root
        for i in range(searches)
    ]
    await session.commit()
    return user.id, roots


async def subscribe(session: AsyncSession, user_id: int, root: int, *, days: int = 20) -> None:
    session.add(
        models.Subscription(
            user_id=user_id,
            passport_root=root,
            is_active=True,
            expires_at=datetime.now(UTC) + timedelta(days=days),
            charge_id=f"charge-{root}",
            since_listing_id=1,
            scan_listing_id=1,
            failed_streak=3,
            last_error="boom",
        )
    )
    await session.commit()


async def test_an_edit_makes_exactly_one_new_version_with_a_manual_edit_event(
    db_session: AsyncSession,
) -> None:
    user_id, (root,) = await user_with(db_session, 1)
    editor = PassportEditor(db_session)

    stored = await editor.edit(
        user_id=user_id,
        root=root,
        base_version=1,
        changes=[Change("transmission", "manual"), Change("brand")],
    )
    await db_session.commit()

    versions = await PassportRepository(db_session).list_versions(root)
    assert [v.version for v in versions] == [1, 2]
    assert [v.is_current for v in versions] == [False, True]
    assert stored.passport.attributes == {"transmission": "manual"}
    events = await PassportRepository(db_session).list_events(root)
    assert events[-1].kind == "manual_edit"
    assert events[-1].payload == {"set": {"transmission": "manual"}, "removed": ["brand"]}


async def test_an_edit_on_a_stale_version_is_refused_and_changes_nothing(
    db_session: AsyncSession,
) -> None:
    user_id, (root,) = await user_with(db_session, 2)
    editor = PassportEditor(db_session)
    await editor.edit(user_id=user_id, root=root, base_version=1, changes=[Change("brand")])
    await db_session.commit()

    with pytest.raises(StaleVersion) as stale:
        await editor.edit(
            user_id=user_id, root=root, base_version=1, changes=[Change("transmission", "manual")]
        )

    assert stale.value.current.version == 2
    assert [v.version for v in await PassportRepository(db_session).list_versions(root)] == [1, 2]


async def test_an_invalid_edit_leaves_the_chain_alone(db_session: AsyncSession) -> None:
    user_id, (root,) = await user_with(db_session, 3)
    with pytest.raises(EditError):
        await PassportEditor(db_session).edit(
            user_id=user_id, root=root, base_version=1, changes=[Change("city")]
        )
    assert len(await PassportRepository(db_session).list_versions(root)) == 1


async def test_someone_elses_search_is_not_editable(db_session: AsyncSession) -> None:
    _, (root,) = await user_with(db_session, 4)
    stranger, _ = await user_with(db_session, 5, searches=0)
    with pytest.raises(LookupError):
        await PassportEditor(db_session).edit(
            user_id=stranger, root=root, base_version=1, changes=[Change("brand")]
        )


async def test_archive_hides_the_search_pauses_its_slot_and_keeps_the_versions(
    db_session: AsyncSession,
) -> None:
    user_id, (keep, gone) = await user_with(db_session, 6, searches=2)
    await subscribe(db_session, user_id, gone)
    passports = PassportRepository(db_session)

    assert await WatchRepository(db_session).archive(user_id, gone)
    await db_session.commit()

    assert [q.root for q in await passports.list_queries(user_id)] == [keep]
    assert await passports.get_query(user_id, gone) is None
    current = await passports.get_current(user_id)
    assert current is not None and current.root == keep
    assert len(await passports.list_versions(gone)) == 1
    sub = await db_session.scalar(select(models.Subscription))
    assert sub is not None and sub.is_active is False
    assert await WatchRepository(db_session).count_searches(user_id) == 1


async def test_archive_is_idempotent_and_refuses_a_stranger(db_session: AsyncSession) -> None:
    user_id, (root,) = await user_with(db_session, 7)
    stranger, _ = await user_with(db_session, 8, searches=0)
    watch = WatchRepository(db_session)

    assert not await watch.archive(stranger, root)
    assert await watch.archive(user_id, root)
    assert await watch.archive(user_id, root)
    await db_session.commit()
    rows = (await db_session.scalars(select(models.SearchTab))).all()
    assert [(r.passport_root, r.state) for r in rows] == [(root, "archived")]


async def test_an_expired_slot_is_not_a_slot(db_session: AsyncSession) -> None:
    user_id, (root,) = await user_with(db_session, 14)
    await subscribe(db_session, user_id, root, days=-1)
    assert await WatchRepository(db_session).live_slots(user_id, datetime.now(UTC)) == []


async def test_an_action_inside_a_topic_does_not_move_the_general_chat_pointer(
    db_session: AsyncSession,
) -> None:
    user_id, (general, other) = await user_with(db_session, 15, searches=2)
    passports = PassportRepository(db_session)
    assert await passports.select(user_id, general)

    await passports.select(user_id, other, editing=True, move_pointer=False)
    await db_session.commit()

    user = await db_session.get(models.User, user_id, populate_existing=True)
    assert user is not None
    assert user.active_passport_root == general
    assert user.editing_passport_root == other
