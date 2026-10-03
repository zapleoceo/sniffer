"""Ранг слота и выбор порции — одно условие: форма запросов без базы (ревью Opus, P5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects import postgresql

from sniffer.db.repositories import monitors

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def text(statement: Any) -> str:
    compiled = statement.compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    return " ".join(str(compiled).split())


def test_the_rank_skips_quarantined_slots_and_chains_without_a_current_passport() -> None:
    sql = text(monitors._ranked_statement([1, 2], now=NOW))
    assert "quarantined_until <=" in sql
    assert "passports.is_current" in sql
    assert "bot_blocked_at" in sql
    assert "ORDER BY subscriptions.user_id, subscriptions.priority, subscriptions.id" in sql


def test_the_rank_and_the_portion_share_one_condition() -> None:
    ranked = text(monitors._ranked_statement([1], now=NOW))
    due = text(monitors._due_statement(limit=5, now=NOW))
    for condition in (
        "quarantined_until <=",
        "passports.is_current",
        "bot_blocked_at",
        "expires_at",
    ):
        assert condition in ranked and condition in due, condition


def test_the_lapse_mark_covers_expired_and_switched_off_slots_and_never_overwrites() -> None:
    sql = text(monitors._lapse_statement(now=NOW))
    assert "no_slot_since IS NULL" in sql, "начало паузы пишется один раз"
    assert "subscriptions.is_active IS false" in sql
    assert "subscriptions.expires_at <=" in sql
    assert "CASE WHEN (subscriptions.expires_at <=" in sql, "пауза началась с конца срока"
