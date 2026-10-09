"""Исключение группы на живом Postgres: транзакция, журнал, курсоры, чтение, потолок (019).

Пропускается без `TEST_DATABASE_URL` (см. `conftest.py`). Время — настоящее «сейчас»
(правило `test_db_clock_rule.py`).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sniffer.db import collection_models as _collection_models  # noqa: F401
from sniffer.db import models
from sniffer.db.repositories import ChatRepository, RawMessageRepository
from sniffer.db.repositories.chat_exclusion import ChatExclusionRepository, Outcome
from sniffer.db.repositories.collection_sources import CollectionSourceRepository
from sniffer.domain.records import Chat, RawMessage

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL не задан: живого Postgres нет",
)

EVIDENCE = {
    "period": "2026-08-31..2026-10-09",
    "messages": 1840,
    "passed_gate": 12,
    "useful_offers": 0,
    "unchecked": 0,
    "backfill_done": True,
    "coverage": "full",
}


async def _chat(session: AsyncSession, tg_id: int, **fields: object) -> None:
    await ChatRepository(session).add(
        Chat(tg_id=tg_id, title=f"чат {tg_id}", city="nha_trang", **fields)  # type: ignore[arg-type]
    )
    await session.commit()


async def _events(session: AsyncSession, tg_id: int) -> list[models.ChatExclusionEvent]:
    return await ChatExclusionRepository(session).history(tg_id)


async def test_exclude_flips_the_chat_and_writes_one_event_together(
    db_session: AsyncSession,
) -> None:
    await _chat(db_session, -1001, last_msg_id=500, username="dead_chat")
    repo = ChatExclusionRepository(db_session)

    outcome = await repo.exclude(
        -1001, reason="zero_useful", evidence=EVIDENCE, actor="owner", now=datetime.now(UTC)
    )
    await db_session.commit()

    assert outcome is Outcome.DONE
    chat = await db_session.scalar(select(models.Chat).where(models.Chat.tg_id == -1001))
    assert chat is not None
    await db_session.refresh(chat)
    assert chat.is_active is False
    assert chat.excluded_at is not None
    assert chat.excluded_reason == "zero_useful"
    assert chat.excluded_evidence is not None
    assert chat.excluded_evidence["useful_offers"] == 0
    assert chat.excluded_evidence["cursors"]["last_msg_id"] == 500
    events = await _events(db_session, -1001)
    assert [(e.action, e.reason, e.actor) for e in events] == [("exclude", "zero_useful", "owner")]
    assert events[0].evidence == chat.excluded_evidence


async def test_a_rolled_back_exclusion_leaves_neither_a_flag_nor_an_event(
    db_session: AsyncSession,
) -> None:
    await _chat(db_session, -1002)
    await ChatExclusionRepository(db_session).exclude(
        -1002, reason="zero_useful", evidence=EVIDENCE, actor="owner"
    )
    await db_session.rollback()

    chat = await ChatRepository(db_session).get_by_tg_id(-1002)
    assert chat is not None and chat.is_active and chat.excluded_at is None
    assert await _events(db_session, -1002) == []


async def test_a_repeated_exclude_is_a_noop_without_a_second_event(
    db_session: AsyncSession,
) -> None:
    await _chat(db_session, -1003)
    repo = ChatExclusionRepository(db_session)
    first = await repo.exclude(-1003, reason="zero_useful", evidence=EVIDENCE, actor="owner")
    await db_session.commit()
    again = await repo.exclude(-1003, reason="other", evidence=EVIDENCE, actor="owner")
    await db_session.commit()

    assert (first, again) == (Outcome.DONE, Outcome.NOOP)
    events = await _events(db_session, -1003)
    assert len(events) == 1 and events[0].reason == "zero_useful"


async def test_an_unknown_chat_is_not_found_and_leaves_no_event(db_session: AsyncSession) -> None:
    repo = ChatExclusionRepository(db_session)
    assert await repo.exclude(-9, reason="x", evidence=EVIDENCE, actor="owner") is Outcome.NOT_FOUND
    assert await repo.restore(-9, reason="x", actor="owner") is Outcome.NOT_FOUND
    assert await _events(db_session, -9) == []


async def test_restore_brings_the_chat_back_and_keeps_every_cursor(
    db_session: AsyncSession,
) -> None:
    await _chat(db_session, -1004, last_msg_id=900)
    await ChatRepository(db_session).mark_backfilled(-1004, oldest_msg_id=120, done=False)
    await db_session.commit()
    repo = ChatExclusionRepository(db_session)
    await repo.exclude(-1004, reason="zero_useful", evidence=EVIDENCE, actor="owner")
    await db_session.commit()

    outcome = await repo.restore(-1004, reason="ошиблись", actor="owner")
    await db_session.commit()

    assert outcome is Outcome.DONE
    chat = await ChatRepository(db_session).get_by_tg_id(-1004)
    assert chat is not None
    assert chat.is_active is True and chat.excluded_at is None
    assert (chat.last_msg_id, chat.backfill_msg_id, chat.backfill_done) == (900, 120, False)
    row = await db_session.scalar(select(models.Chat).where(models.Chat.tg_id == -1004))
    assert row is not None
    await db_session.refresh(row)
    assert row.excluded_reason is None and row.excluded_evidence is None
    # Именно SQL NULL, а не JSON `null`: по нему отбирают исключённых.
    cleared = await db_session.scalar(
        select(func.count(models.Chat.id)).where(
            models.Chat.tg_id == -1004,
            models.Chat.excluded_reason.is_(None),
            models.Chat.excluded_evidence.is_(None),
        )
    )
    assert cleared == 1
    events = await _events(db_session, -1004)
    assert [e.action for e in events] == ["exclude", "restore"]
    assert events[1].evidence is not None
    assert events[1].evidence["was_reason"] == "zero_useful"
    assert events[1].evidence["was_evidence"]["messages"] == 1840
    # Повторный restore — тот же безопасный повтор.
    assert await repo.restore(-1004, reason="ещё раз", actor="owner") is Outcome.NOOP
    assert len(await _events(db_session, -1004)) == 2


async def test_an_excluded_chat_is_read_by_no_collector_path(db_session: AsyncSession) -> None:
    await _chat(db_session, -1005, search_rank=1)
    await _chat(db_session, -1006, search_rank=2)
    await ChatExclusionRepository(db_session).exclude(
        -1005, reason="zero_useful", evidence=EVIDENCE, actor="owner"
    )
    await db_session.commit()
    chats = ChatRepository(db_session)

    assert [c.tg_id for c in await chats.list_active()] == [-1006]  # история, живой поиск
    backfill = await chats.next_backfill()
    assert backfill is not None and backfill.tg_id == -1006  # архив
    assert -1005 in [c.tg_id for c in await chats.list_all()]  # владельцу в реестре виден
    assert await chats.is_excluded(tg_id=-1005) is True
    assert await chats.is_excluded(tg_id=-1006) is False
    assert await chats.has_identity(tg_id=-1005) is True  # строка есть: кандидата не заведут


async def test_the_archive_search_stops_reading_an_excluded_chat(db_session: AsyncSession) -> None:
    await ChatRepository(db_session).add(
        Chat(tg_id=-1007, title="байки", city="nha_trang", username="bikes_nha")
    )
    raw_ids = await RawMessageRepository(db_session).add_many(
        [
            RawMessage(
                chat_tg_id=-1007,
                msg_id=7,
                text="Аренда байков в Нячанге, Honda Vision",
                text_hash="h-7",
                posted_at=datetime.now(UTC),
            )
        ]
    )
    await RawMessageRepository(db_session).set_stage(
        raw_ids,
        "extracted",
        gate_signals={"is_offer": True, "categories": ["motorbike"], "reason": "ok"},
    )
    await db_session.commit()
    scope = {"city": "nha_trang", "category": "motorbike", "deal_type": "rent_out", "criteria": {}}
    archive = CollectionSourceRepository(db_session)
    assert [row["msg_id"] for row in await archive.archive(scope)] == [7]

    await ChatExclusionRepository(db_session).exclude(
        -1007, reason="zero_useful", evidence=EVIDENCE, actor="owner"
    )
    await db_session.commit()

    assert await archive.archive(scope) == []
    assert await db_session.scalar(select(func.count(models.RawMessage.id))) == 1  # история цела


async def test_excluded_chats_do_not_take_room_in_the_cap(db_session: AsyncSession) -> None:
    for number in range(5):
        await _chat(db_session, -2000 - number)
    repo = ChatExclusionRepository(db_session)
    for number in range(3):
        await repo.exclude(-2000 - number, reason="zero_useful", evidence=EVIDENCE, actor="owner")
    await db_session.commit()

    chats = ChatRepository(db_session)
    assert await chats.count() == 2
    total = await db_session.scalar(select(func.count(models.Chat.id)))
    assert total == 5


async def test_a_chat_switched_off_by_hand_still_holds_its_place(db_session: AsyncSession) -> None:
    """Не-исключённый `is_active=false` в потолке остаётся: его статус никто не решал."""
    await _chat(db_session, -3001, is_active=False)
    await _chat(db_session, -3002)
    assert await ChatRepository(db_session).count() == 2


async def test_the_journal_survives_in_a_second_session(db_engine: AsyncEngine) -> None:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as session:
        await _chat(session, -4001)
        await ChatExclusionRepository(session).exclude(
            -4001, reason="zero_useful", evidence=EVIDENCE, actor="owner"
        )
        await session.commit()
    async with sessions() as other:
        assert [e.action for e in await _events(other, -4001)] == ["exclude"]
