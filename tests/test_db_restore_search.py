"""Возврат убранного поиска владельцем на живом Postgres (в CI; локально пропускается).

Держится на свойствах самой базы (условный `UPDATE … RETURNING`, блокировка клиента) и на том,
что настоящий проход монитора после возврата не вываливает накопленное за архив. Время —
настоящее «сейчас» плюс смещения (правило `test_db_clock_rule.py`).
"""

from __future__ import annotations

import argparse
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db import models
from sniffer.db.models import tabs
from sniffer.db.repositories import (
    ListingRepository,
    PassportRepository,
    RawMessageRepository,
    UserRepository,
)
from sniffer.db.repositories.passports import not_archived
from sniffer.db.repositories.slots import PaidSearch, SlotRepository
from sniffer.db.repositories.tabs import TabRepository
from sniffer.db.repositories.watch import WatchRepository
from sniffer.domain.hard_filter import BALCONY, HardFilter
from sniffer.domain.passport import Category, Intent, Passport
from sniffer.domain.records import Listing, RawMessage
from sniffer.worker import restore_search
from sniffer.worker.monitor import MonitorAgent

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL не задан: живого Postgres нет",
)

SPEC = HardFilter(require=frozenset({BALCONY}), districts=frozenset({"vinh_hoa"}))


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@asynccontextmanager
async def _borrowed(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    yield session


def _passport(city: str = "nha_trang") -> Passport:
    return Passport(intent=Intent.BUY, category=Category.MOTORBIKE, city=city, raw_query="байк")


async def _owner(session: AsyncSession, tg_id: int) -> tuple[int, int]:
    """Клиент и корень его поиска, на который выдано бессрочное слежение с жёстким фильтром."""
    user = await UserRepository(session).get_or_create(tg_id)
    assert user.id is not None
    stored = await PassportRepository(session).save_new(user.id, _passport())
    await SlotRepository(session).grant(
        user.id, stored.root, hard_filter=None, lookback=timedelta(0), now=_now()
    )
    await session.execute(
        update(models.Subscription)
        .where(models.Subscription.user_id == user.id)
        .values(hard_filter=SPEC.to_json(), since_listing_id=0, scan_listing_id=0)
    )
    await session.commit()
    return user.id, stored.root


async def _archive(session: AsyncSession, user_id: int, root: int, since: datetime | None) -> None:
    """Как «Удалить поиск»; отметку паузы ставит монитор, поэтому её задаём вручную."""
    assert await WatchRepository(session).archive(user_id, root)
    await session.execute(
        update(models.Subscription)
        .where(models.Subscription.user_id == user_id)
        .values(no_slot_since=since)
    )
    await session.commit()


async def _restore(session: AsyncSession, user_id: int, root: int) -> None:
    assert await WatchRepository(session).restore_archived(user_id, root)
    await SlotRepository(session).resume_restored(user_id, root, now=_now())
    await session.commit()


async def _sub(session: AsyncSession, user_id: int) -> models.Subscription:
    row = await session.scalar(
        select(models.Subscription)
        .where(models.Subscription.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    assert row is not None
    return row


async def _card(session: AsyncSession, number: int) -> Listing:
    posted = _now() - timedelta(hours=1)
    (raw_id,) = await RawMessageRepository(session).add_many(
        [
            RawMessage(
                chat_tg_id=-100999,
                msg_id=number,
                text=f"Продам байк {number}",
                text_hash=f"restore-{number}",
                posted_at=posted,
            )
        ]
    )
    card = await ListingRepository(session).add(
        Listing(
            raw_message_id=raw_id,
            deal_type="sell",
            category="motorbike",
            city="nha_trang",
            title=f"Байк {number}",
            summary="Автомат",
            tg_link=f"https://t.me/c/1/{number}",
            posted_at=posted,
        )
    )
    await session.execute(
        update(models.Listing).where(models.Listing.id == card.id).values(screened_at=_now())
    )
    return card


async def test_restore_turns_archived_into_lost_and_resumes_monitoring_without_touching_the_filter(
    db_session: AsyncSession,
) -> None:
    user_id, root = await _owner(db_session, 880_001)
    await _archive(db_session, user_id, root, _now() - timedelta(days=3))
    before = await _sub(db_session, user_id)
    kept = (before.hard_filter, before.since_listing_id, before.scan_listing_id, before.priority)
    paused_since = before.no_slot_since
    assert not before.is_active

    assert await WatchRepository(db_session).restore_archived(user_id, root)
    resumed = await SlotRepository(db_session).resume_restored(user_id, root, now=_now())
    await db_session.commit()

    assert resumed == before.id
    assert await TabRepository(db_session).tab_of(user_id, root) == (None, None, tabs.LOST)
    after = await _sub(db_session, user_id)
    assert after.is_active and after.expires_at is None
    assert (
        after.hard_filter,
        after.since_listing_id,
        after.scan_listing_id,
        after.priority,
    ) == kept
    assert after.no_slot_since == paused_since, "пауза не сбрасывается: курсор поднимет монитор"
    visible = await db_session.scalar(
        select(func.count()).select_from(models.Passport).where(not_archived())
    )
    assert visible, "поиск снова виден в списках"
    # `attach` — штатный путь, которым бот пересоздаёт тему: убранному поиску тема не давалась.
    assert await TabRepository(db_session).attach(user_id, root, 4242, "Поиск")


async def test_restore_twice_is_a_no_op_the_second_time(db_session: AsyncSession) -> None:
    user_id, root = await _owner(db_session, 880_002)
    await _archive(db_session, user_id, root, _now() - timedelta(days=3))
    await _restore(db_session, user_id, root)
    snapshot = await _sub(db_session, user_id)
    state = (snapshot.is_active, snapshot.no_slot_since, snapshot.scan_listing_id)

    assert not await WatchRepository(db_session).restore_archived(user_id, root), "уже не в архиве"
    again = await _sub(db_session, user_id)
    assert (again.is_active, again.no_slot_since, again.scan_listing_id) == state


async def test_restore_never_touches_another_users_or_another_roots_archive(
    db_session: AsyncSession,
) -> None:
    mine, my_root = await _owner(db_session, 880_003)
    other, other_root = await _owner(db_session, 880_004)
    spare = (
        await PassportRepository(db_session).save_new(mine, _passport("hue"), move_pointer=False)
    ).root
    for user, root in ((mine, my_root), (other, other_root), (mine, spare)):
        await WatchRepository(db_session).archive(user, root)
    await db_session.commit()

    assert not await WatchRepository(db_session).restore_archived(mine, other_root), "чужой корень"
    assert not await WatchRepository(db_session).restore_archived(other, my_root)
    assert await WatchRepository(db_session).restore_archived(mine, my_root)
    await db_session.commit()

    states = {
        (row.user_id, row.passport_root): row.state
        for row in await db_session.scalars(select(models.SearchTab))
    }
    assert states[(mine, my_root)] == tabs.LOST
    assert states[(other, other_root)] == tabs.ARCHIVED
    assert states[(mine, spare)] == tabs.ARCHIVED, "другая цепочка того же клиента не тронута"


async def test_a_search_without_monitoring_is_restored_without_inventing_one(
    db_session: AsyncSession,
) -> None:
    user = await UserRepository(db_session).get_or_create(880_005)
    assert user.id is not None
    root = (await PassportRepository(db_session).save_new(user.id, _passport("x"))).root
    await WatchRepository(db_session).archive(user.id, root)
    await db_session.commit()

    assert await WatchRepository(db_session).restore_archived(user.id, root)
    assert await SlotRepository(db_session).resume_restored(user.id, root, now=_now()) is None
    assert not await db_session.scalar(select(func.count()).select_from(models.Subscription))


async def test_after_a_long_archive_the_first_pass_jumps_to_now_and_sends_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sniffer.worker import monitor as module

    user_id, root = await _owner(db_session, 880_006)
    # Без жёсткого фильтра: если бы курсор не прыгнул, карточка бы ушла, и тест это увидел бы.
    await db_session.execute(
        update(models.Subscription)
        .where(models.Subscription.user_id == user_id)
        .values(hard_filter=None)
    )
    await _archive(db_session, user_id, root, _now() - timedelta(days=3))
    old = await _card(db_session, 1)
    await db_session.commit()
    assert old.id is not None
    await _restore(db_session, user_id, root)
    monkeypatch.setattr(module, "session_scope", lambda: _borrowed(db_session))

    assert await MonitorAgent().tick(now=_now()) == 0, "накопившееся за архив не вываливается"

    after = await _sub(db_session, user_id)
    assert after.scan_listing_id >= old.id and after.no_slot_since is None
    assert not await db_session.scalar(select(func.count()).select_from(models.Notification))


async def test_the_restore_keeps_the_hard_filter_the_monitor_reads(
    db_session: AsyncSession,
) -> None:
    from sniffer.db.repositories.monitors import MonitorRepository

    user_id, root = await _owner(db_session, 880_012)
    await _archive(db_session, user_id, root, _now() - timedelta(days=3))
    await _restore(db_session, user_id, root)

    due = await MonitorRepository(db_session).claim_due(limit=500, now=_now())

    assert [item.hard_filter for item in due.ready if item.user_id == user_id] == [SPEC]


async def test_after_a_short_archive_the_cursor_stays_where_it_was(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Короткая пауза курсор не двигает — то же правило, что у возврата платного слота."""
    from sniffer.worker import monitor as module

    user_id, root = await _owner(db_session, 880_007)
    await db_session.execute(
        update(models.Subscription)
        .where(models.Subscription.user_id == user_id)
        .values(hard_filter=None)
    )
    await _archive(db_session, user_id, root, _now() - timedelta(hours=2))
    await _card(db_session, 2)
    await db_session.commit()
    await _restore(db_session, user_id, root)
    monkeypatch.setattr(module, "session_scope", lambda: _borrowed(db_session))

    assert await MonitorAgent().tick(now=_now()) == 1
    assert (await _sub(db_session, user_id)).no_slot_since is None


async def test_without_a_pause_mark_the_restore_itself_lifts_the_cursor(
    db_session: AsyncSession,
) -> None:
    """Строка остановлена, а монитор отметку паузы ещё не поставил: курсор не остаётся старым."""
    user_id, root = await _owner(db_session, 880_008)
    await _archive(db_session, user_id, root, None)
    card = await _card(db_session, 3)
    await db_session.commit()
    assert card.id is not None

    await _restore(db_session, user_id, root)

    row = await _sub(db_session, user_id)
    assert row.scan_listing_id >= card.id and row.is_active
    assert row.since_listing_id == 0, "точка начала не переписывается"


async def test_the_command_reports_foreign_and_missing_roots_as_not_found_and_writes_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, my_root = await _owner(db_session, 880_009)
    other, other_root = await _owner(db_session, 880_010)
    await WatchRepository(db_session).archive(other, other_root)
    await db_session.commit()
    monkeypatch.setattr(restore_search, "session_scope", lambda: _borrowed(db_session))
    monkeypatch.setattr(
        restore_search, "get_settings", lambda: SimpleNamespace(is_owner=lambda _: True)
    )

    def args(tg: int, root: int) -> argparse.Namespace:
        return argparse.Namespace(tg_user_id=tg, root=root)

    now = _now()
    assert await restore_search.restore(args(880_009, other_root), now) == 3, "чужой корень"
    assert await restore_search.restore(args(880_009, 999_999), now) == 3, "корня нет"
    assert await restore_search.restore(args(1, my_root), now) == 3, "клиента нет"
    assert await restore_search.restore(args(880_009, my_root), now) == 0, "не в архиве: no-op"

    states = {row.user_id: row.state for row in await db_session.scalars(select(models.SearchTab))}
    assert states == {other: tabs.ARCHIVED}, "ни одна строка не изменилась"
    assert not await db_session.scalar(
        select(func.count()).select_from(models.User).where(models.User.tg_user_id == 1)
    ), "незнакомый клиент не создаётся"


async def test_the_command_restores_the_owners_own_search(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, root = await _owner(db_session, 880_011)
    await _archive(db_session, user_id, root, _now() - timedelta(days=3))
    monkeypatch.setattr(restore_search, "session_scope", lambda: _borrowed(db_session))
    monkeypatch.setattr(
        restore_search, "get_settings", lambda: SimpleNamespace(is_owner=lambda _: True)
    )

    code = await restore_search.restore(argparse.Namespace(tg_user_id=880_011, root=root), _now())

    assert code == 0
    assert (await _sub(db_session, user_id)).is_active
    assert (await TabRepository(db_session).tab_of(user_id, root)) == (None, None, tabs.LOST)


async def test_a_paid_search_archived_for_real_is_refused_and_left_archived(
    db_session: AsyncSession,
) -> None:
    """Настоящий архив: `sync` ставит оплаченной строке срок на момент архива — обнулять нельзя."""
    user_id, root = await _owner(db_session, 880_013)
    await db_session.execute(
        update(models.Subscription)
        .where(models.Subscription.user_id == user_id)
        .values(expires_at=_now() + timedelta(days=20))
    )
    await db_session.commit()
    assert await WatchRepository(db_session).archive(user_id, root)
    await SlotRepository(db_session).sync(user_id, _now())
    await db_session.commit()
    before = await _sub(db_session, user_id)
    assert before.expires_at is not None and not before.is_active

    assert await WatchRepository(db_session).restore_archived(user_id, root)
    with pytest.raises(PaidSearch):
        await SlotRepository(db_session).resume_restored(user_id, root, now=_now())
    await db_session.rollback()

    after = await _sub(db_session, user_id)
    assert (after.is_active, after.expires_at) == (False, before.expires_at)
    assert (await TabRepository(db_session).tab_of(user_id, root)) == (None, None, tabs.ARCHIVED)


async def test_a_search_with_a_saved_topic_comes_back_open_in_that_topic(
    db_session: AsyncSession,
) -> None:
    user_id, root = await _owner(db_session, 880_014)
    assert await TabRepository(db_session).claim(user_id, root, 777, "Квартира")
    await _archive(db_session, user_id, root, _now() - timedelta(days=3))
    assert await TabRepository(db_session).root_of(user_id, 777) is None, "в архиве тема молчит"

    assert await WatchRepository(db_session).restore_archived(user_id, root) == tabs.OPEN
    await SlotRepository(db_session).resume_restored(user_id, root, now=_now())
    await db_session.commit()

    assert await TabRepository(db_session).root_of(user_id, 777) == root
    assert await TabRepository(db_session).tab_of(user_id, root) == (777, "Квартира", tabs.OPEN)


async def test_the_command_refuses_a_non_owner_even_for_their_own_archived_search(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, root = await _owner(db_session, 880_015)
    await _archive(db_session, user_id, root, _now() - timedelta(days=3))
    monkeypatch.setattr(restore_search, "session_scope", lambda: _borrowed(db_session))
    monkeypatch.setattr(
        restore_search, "get_settings", lambda: SimpleNamespace(is_owner=lambda _: False)
    )

    code = await restore_search.restore(argparse.Namespace(tg_user_id=880_015, root=root), _now())

    assert code == 3
    assert (await TabRepository(db_session).tab_of(user_id, root)) == (None, None, tabs.ARCHIVED)
