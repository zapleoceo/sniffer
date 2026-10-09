"""Приоритизация очереди вступлений: выключена по умолчанию, обратима, без голодания."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.collector.preview_scan import scan_previews
from sniffer.config import Settings
from sniffer.db.repositories import CandidateRepository
from sniffer.domain.chat_preview import FOREIGN_CITY, OFF_TOPIC, RELEVANT, PreviewSnapshot
from sniffer.domain.join_priority import JoinPriorityPolicy, QueueEntry, order_queue
from sniffer.domain.records import DiscoveryCandidate
from sniffer.sources import telegram_discover_reference as reference

NOW = datetime.now(UTC)
POLICY = JoinPriorityPolicy(unknown_penalty=20, low_penalty=60, aging_hours_per_point=6)


def entry(i: int, cls: str | None, *, priority: int = 100, age_h: float = 0) -> QueueEntry:
    return QueueEntry(i, priority, NOW - timedelta(hours=age_h), cls)


def ids(entries: list[QueueEntry]) -> list[int]:
    return [e.id for e in entries]


def test_setting_is_off_by_default_and_gives_no_policy() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.join_priority_enabled is False
    assert settings.join_preview_scan_enabled is False
    assert settings.join_priority is None


def test_enabled_setting_builds_the_policy() -> None:
    settings = Settings(_env_file=None, join_priority_enabled=True)  # type: ignore[call-arg]
    assert settings.join_priority == POLICY


def test_without_a_policy_the_order_is_exactly_priority_then_found_at() -> None:
    rows = [
        entry(1, OFF_TOPIC, priority=100, age_h=1),
        entry(2, RELEVANT, priority=100, age_h=5),
        entry(3, None, priority=10, age_h=0),
        entry(4, RELEVANT, priority=100, age_h=9),
    ]
    legacy = sorted(rows, key=lambda e: (e.priority, e.found_at))
    assert order_queue(rows, NOW, None) == legacy
    assert ids(order_queue(rows, NOW, None)) == [3, 4, 2, 1]


def test_enabled_puts_relevant_first_unknown_next_off_topic_last() -> None:
    rows = [entry(1, OFF_TOPIC), entry(2, None), entry(3, RELEVANT), entry(4, FOREIGN_CITY)]
    assert ids(order_queue(rows, NOW, POLICY)) == [3, 2, 1, 4]


def test_manual_waves_keep_their_lead_over_discovered_candidates() -> None:
    rows = [entry(1, RELEVANT, priority=100), entry(2, OFF_TOPIC, priority=10)]
    assert ids(order_queue(rows, NOW, POLICY)) == [2, 1]


def test_old_unknown_overtakes_a_fresh_relevant_so_it_does_not_starve() -> None:
    rows = [entry(1, RELEVANT, age_h=0), entry(2, None, age_h=6 * 25)]
    assert ids(order_queue(rows, NOW, POLICY)) == [2, 1]


def test_old_off_topic_also_eventually_gets_its_turn() -> None:
    rows = [entry(1, RELEVANT, age_h=0), entry(2, OFF_TOPIC, age_h=6 * 70)]
    assert ids(order_queue(rows, NOW, POLICY)) == [2, 1]


def test_priority_never_drops_anyone_from_the_queue() -> None:
    rows = [entry(i, c) for i, c in enumerate([None, OFF_TOPIC, RELEVANT, FOREIGN_CITY])]
    assert sorted(ids(order_queue(rows, NOW, POLICY))) == sorted(ids(rows))


def test_join_limits_are_untouched() -> None:
    assert reference.MAX_JOINS_PER_DAY == 10
    assert reference.MIN_JOIN_PAUSE == timedelta(hours=1)
    assert reference.FLOOD_MIN_STOP == timedelta(hours=12)
    assert reference.JOIN_WINDOW == timedelta(hours=24)


def test_new_modules_never_touch_the_join_ledger() -> None:
    root = Path(__file__).parents[1] / "src" / "sniffer"
    forbidden = {"JoinLedgerRepository", "claim_slot", "record_flood", "ChatJoiner"}
    for rel in ("domain/join_priority.py", "collector/preview_scan.py", "search/chat_preview.py"):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        assert not names & forbidden, (rel, names & forbidden)


async def test_preview_scan_does_nothing_while_its_setting_is_off() -> None:
    called = False

    async def fetch(username: str) -> PreviewSnapshot:
        nonlocal called
        called = True
        return PreviewSnapshot(status="ok")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert await scan_previews(settings, fetch=fetch) == 0
    assert called is False


async def test_repository_reserve_follows_the_policy_only_when_given(
    db_session: AsyncSession,
) -> None:
    repo = CandidateRepository(db_session)
    await repo.push(DiscoveryCandidate(key="@yoga", username="yoga"))
    await repo.push(DiscoveryCandidate(key="@flea", username="flea"))
    await repo.save_preview("@yoga", cls=OFF_TOPIC, evidence="йог", snapshot={})
    await repo.save_preview("@flea", cls=RELEVANT, evidence="барахол", snapshot={})
    await db_session.commit()

    ranked = await repo.reserve(POLICY)
    assert ranked is not None and ranked.key == "@flea"
    await repo.release("@flea")
    legacy = await repo.reserve()
    assert legacy is not None and legacy.key == "@yoga"
