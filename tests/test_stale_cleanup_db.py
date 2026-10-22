"""Уборка и ворота 30 суток на живом Postgres (в CI; локально без Docker — пропуск).

Удаление здесь выполняется только в тестовой базе. Боевой запуск — отдельное
решение владельца (`docs/deploy.md`).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sniffer.bot.quota import QuotaService
from sniffer.db import models
from sniffer.db.repositories import (
    ChatRepository,
    ListingRepository,
    PassportRepository,
    RawMessageRepository,
    UserRepository,
)
from sniffer.domain.passport import Category, Intent, Passport
from sniffer.domain.records import Chat, Listing, MatchFilter, RawMessage
from sniffer.worker import archive as archive_module
from sniffer.worker import stale_cleanup as cli
from sniffer.worker.archive import ArchivePipeline
from sniffer.worker.screening import Screening
from tests.quota_support import T0, Clock, SqlKit, account

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL не задан: живого Postgres нет",
)

NOW = datetime.now(UTC).replace(microsecond=0)
CUTOFF = NOW - timedelta(days=30)
SECOND = timedelta(seconds=1)
CONFIRM = cli.confirmation_for(CUTOFF)


def card(external_id: str, posted_at: datetime, **overrides: Any) -> Listing:
    values: dict[str, Any] = {
        "raw_message_id": None,
        "source": "telegram_archive",
        "external_id": external_id,
        "deal_type": "sell",
        "category": "motorbike",
        "city": "nha_trang",
        "title": f"Байк {external_id}",
        "summary": "продам, документы",
        "tg_link": f"https://t.me/c/1/{external_id}",
        "posted_at": posted_at,
        "attributes": {},
    }
    values.update(overrides)
    return Listing(**values)


@pytest.fixture
def sessions(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def seed(
    sessions: async_sessionmaker[AsyncSession], *items: tuple[str, datetime]
) -> dict[str, int]:
    ids: dict[str, int] = {}
    async with sessions() as session:
        repo = ListingRepository(session)
        for external_id, posted_at in items:
            made = await repo.add(card(external_id, posted_at))
            assert made.id is not None
            ids[external_id] = made.id
        await session.commit()
    return ids


async def alive(sessions: async_sessionmaker[AsyncSession]) -> set[str]:
    async with sessions() as session:
        rows = await session.scalars(select(models.Listing.external_id))
        return {row for row in rows if row is not None}


async def run(
    sessions: async_sessionmaker[AsyncSession],
    *,
    execute: bool,
    batch_size: int = 500,
    confirm: str | None = None,
) -> cli.CleanupReport:
    async def no_sleep(_: float) -> None:
        return None

    return await cli.run_cleanup(
        cli.Options(execute=execute, confirm=confirm, batch_size=batch_size, sleep_s=0),
        cli.CleanupReport(),
        now=lambda: NOW,
        sleep=no_sleep,
        session_factory=sessions,
    )


# ── граница и часовой пояс ──────────────────────────────────────────────────


async def test_boundary_is_strict_to_the_second(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(
        sessions,
        ("before", CUTOFF - SECOND),
        ("exact", CUTOFF),
        ("after", CUTOFF + SECOND),
    )

    report = await run(sessions, execute=True, confirm=CONFIRM)

    assert report.deleted == 1
    assert await alive(sessions) == {"exact", "after"}


async def test_a_plus_seven_timestamp_is_compared_as_an_instant(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    hanoi = timezone(timedelta(hours=7))
    await seed(
        sessions,
        ("old", (CUTOFF - SECOND).astimezone(hanoi)),
        ("kept", (CUTOFF + SECOND).astimezone(hanoi)),
    )

    await run(sessions, execute=True, confirm=CONFIRM)

    assert await alive(sessions) == {"kept"}


# ── сухой прогон, подтверждение, идемпотентность ────────────────────────────


async def test_dry_run_deletes_nothing_and_reports_the_same_counts(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(sessions, *((f"old{n}", CUTOFF - timedelta(days=n + 1)) for n in range(5)))

    dry = await run(sessions, execute=False, batch_size=2)

    assert (dry.selected, dry.deleted, dry.batches) == (5, 0, 3)
    assert len(await alive(sessions)) == 5
    wet = await run(sessions, execute=True, confirm=CONFIRM, batch_size=2)
    assert (wet.selected, wet.deleted, wet.batches) == (5, 5, 3)


async def test_execute_without_the_confirmation_deletes_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(sessions, ("old", CUTOFF - SECOND))

    with pytest.raises(cli.UsageError):
        await run(sessions, execute=True)
    with pytest.raises(cli.UsageError):
        await run(sessions, execute=True, confirm="DELETE-STALE-1999-01-01")

    assert await alive(sessions) == {"old"}


async def test_a_second_run_deletes_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(sessions, ("old", CUTOFF - SECOND), ("fresh", NOW))

    assert (await run(sessions, execute=True, confirm=CONFIRM)).deleted == 1
    again = await run(sessions, execute=True, confirm=CONFIRM)

    assert (again.selected, again.deleted) == (0, 0)
    assert await alive(sessions) == {"fresh"}


# ── что защищено ────────────────────────────────────────────────────────────


async def test_offer_views_protect_the_card_and_are_counted(db_engine: AsyncEngine) -> None:
    kit = SqlKit(db_engine)
    user = await kit.new_user()
    shown, plain = await kit.listings(2)
    admission = await QuotaService(kit.ledger, clock=Clock(T0)).admit(account(user), [shown])
    assert admission.ticket is not None
    old = CUTOFF - timedelta(days=5)
    async with kit.sessions() as session:
        await session.execute(
            text("UPDATE listings SET posted_at = :old WHERE id IN (:a, :b)"),
            {"old": old, "a": shown, "b": plain},
        )
        await session.commit()

    report = await run(kit.sessions, execute=True, confirm=CONFIRM)

    assert (report.deleted, report.skipped_offer_views) == (1, 1)
    async with kit.sessions() as session:
        left = set(await session.scalars(select(models.Listing.id)))
        views = await session.scalar(select(func.count()).select_from(models.OfferView))
    assert left == {shown} and views == 1


async def test_catalog_projections_are_never_deleted(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """`posted_at` проекции мог прийти из `fetched_at` — по дате её не удаляем."""
    ids = await seed(
        sessions, ("projection", CUTOFF - timedelta(days=9)), ("plain", CUTOFF - SECOND)
    )
    async with sessions() as session:
        # Строка наблюдения живёт за цепочкой задач сборщика; для проверки важен
        # лишь сам признак, поэтому внешние ключи на время вставки выключены.
        await session.execute(text("SET LOCAL session_replication_role = replica"))
        await session.execute(
            text(
                "INSERT INTO catalog_observations (id, task_id, source, external_id, "
                "content_hash, fetched_at, payload, created_at) "
                "VALUES (9001, 1, 'chotot', 'x', 'h', now(), '{}', now())"
            )
        )
        await session.execute(
            text("UPDATE listings SET catalog_observation_id = 9001 WHERE id = :id"),
            {"id": ids["projection"]},
        )
        await session.commit()

    report = await run(sessions, execute=True, confirm=CONFIRM)

    assert (report.deleted, report.skipped_catalog) == (1, 1)
    assert await alive(sessions) == {"projection"}


async def test_media_and_notifications_go_with_the_card_and_raw_messages_stay(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    old = CUTOFF - timedelta(days=3)
    async with sessions() as session:
        (raw_id,) = await RawMessageRepository(session).add_many(
            [
                RawMessage(
                    chat_tg_id=-100123, msg_id=1, text="Продам байк", text_hash="h", posted_at=old
                )
            ]
        )
        listing = await ListingRepository(session).add(card("old", old, raw_message_id=raw_id))
        assert listing.id is not None
        user = await UserRepository(session).get_or_create(9101)
        assert user.id is not None
        passport = await PassportRepository(session).save_new(
            user.id,
            Passport(
                intent=Intent.BUY, category=Category.MOTORBIKE, city="nha_trang", raw_query="q"
            ),
        )
        sub = models.Subscription(user_id=user.id, passport_root=passport.id)
        session.add(sub)
        await session.flush()
        session.add(models.ListingMedia(listing_id=listing.id, r2_key="k1"))
        session.add(models.ListingMedia(listing_id=listing.id, r2_key="k2"))
        session.add(models.Notification(subscription_id=sub.id, listing_id=listing.id, score=0.5))
        await session.commit()

    report = await run(sessions, execute=True, confirm=CONFIRM)

    assert (report.deleted, report.media, report.notifications) == (1, 2, 1)
    async with sessions() as session:
        for model in (models.Listing, models.ListingMedia, models.Notification):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        assert await session.scalar(select(func.count()).select_from(models.RawMessage)) == 1


async def test_a_stale_but_active_card_is_still_served_until_expiry_runs(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Ворота не добавили фильтров в выдачу: это работа `expire`, а не уборки/ворот."""
    await seed(sessions, ("old", NOW - timedelta(days=40)), ("fresh", NOW))
    async with sessions() as session:
        found = await ListingRepository(session).search_catalog(
            MatchFilter(city="nha_trang", category="motorbike")
        )
    assert {row.external_id for row in found} == {"old", "fresh"}


# ── ИИ-проверка ─────────────────────────────────────────────────────────────


async def test_screening_never_picks_or_pays_for_a_stale_card(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(sessions, ("old", NOW - timedelta(days=31)), ("edge", NOW - timedelta(days=29)))
    calls: list[object] = []

    class Broker:
        async def structured(self, *args: Any, **kwargs: Any) -> Any:
            calls.append(args)
            raise AssertionError("брокер не должен вызываться за стухшую карточку")

    async with sessions() as session:
        queue = await ListingRepository(session).unscreened("telegram_archive", limit=10)
    assert [row.external_id for row in queue] == ["edge"]

    async def real_queue(limit: int) -> list[Listing]:
        async with sessions() as session:
            return await ListingRepository(session).unscreened("telegram_archive", limit=limit)

    async def only_stale(limit: int) -> list[Listing]:
        return [row for row in await real_queue(limit) if row.external_id == "old"]

    done = await Screening(broker=Broker(), unscreened=only_stale).tick()
    assert (done, calls) == (0, [])


# ── воронка: репосты ────────────────────────────────────────────────────────

TEXT = "Продам Honda Vision 2021, цена 15.000.000 VND, документы есть"


@asynccontextmanager
async def borrowed(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    yield session


async def tick_with(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, *messages: RawMessage
) -> None:
    await RawMessageRepository(session).add_many(list(messages))
    await session.commit()
    monkeypatch.setattr(archive_module, "session_scope", lambda: borrowed(session))
    await ArchivePipeline(now=lambda: NOW).tick()


def message(chat: int, msg_id: int, posted_at: datetime) -> RawMessage:
    return RawMessage(
        chat_tg_id=chat, msg_id=msg_id, text=TEXT, text_hash="raw-hash", posted_at=posted_at
    )


async def two_chats(session: AsyncSession) -> None:
    for tg_id, name in ((-100111, "a"), (-100222, "b")):
        await ChatRepository(session).add(
            Chat(tg_id=tg_id, title=name, city="nha_trang", username=name)
        )
    await session.commit()


async def test_a_same_chat_repost_with_a_later_date_refreshes_the_card(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await two_chats(db_session)
    await tick_with(db_session, monkeypatch, message(-100111, 1, NOW - timedelta(days=5)))
    await tick_with(db_session, monkeypatch, message(-100111, 2, NOW - timedelta(days=1)))

    rows = list(await db_session.scalars(select(models.Listing)))
    assert len(rows) == 1
    assert rows[0].posted_at == NOW - timedelta(days=1)


async def test_cross_chat_repost_with_the_same_text_refreshes_too_pinned_known_risk(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ЗАФИКСИРОВАННОЕ ТЕКУЩЕЕ ПОВЕДЕНИЕ, не доказательство нового продавца.

    Совпадение отпечатка текста в ДРУГОМ чате поднимает ту же карточку на новую
    дату: «свежесть» от репоста — не свидетельство актуальности объявления
    (docs/architecture.md, «Известный риск»). Этот тест ломается, если поведение
    сменят, — и тогда документ надо править вместе с ним.
    """
    await two_chats(db_session)
    await tick_with(db_session, monkeypatch, message(-100111, 1, NOW - timedelta(days=5)))
    await tick_with(db_session, monkeypatch, message(-100222, 1, NOW - timedelta(days=1)))

    rows = list(await db_session.scalars(select(models.Listing)))
    assert len(rows) == 1
    assert rows[0].posted_at == NOW - timedelta(days=1)
    second = await RawMessageRepository(db_session).get_by_key(-100222, 1)
    assert second is not None and second.stage == "extracted"
    assert rows[0].raw_message_id == second.id


async def test_stale_messages_neither_create_nor_resurrect_a_card(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await two_chats(db_session)
    await tick_with(db_session, monkeypatch, message(-100111, 1, NOW - timedelta(days=60)))
    await tick_with(db_session, monkeypatch, message(-100222, 1, NOW - timedelta(days=40)))

    assert await db_session.scalar(select(func.count()).select_from(models.Listing)) == 0
    stages = set(await db_session.scalars(select(models.RawMessage.stage)))
    assert stages == {"stale"}
