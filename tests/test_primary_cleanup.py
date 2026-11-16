"""Уборка первоисточников и дублей: план проверенного каталога и коды возврата CLI."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

import sniffer.worker.primary_cleanup as primary_cleanup

NOW = datetime(2026, 11, 16, tzinfo=UTC)
TEXT = "Сдаётся студия в Нячанге, район Тхап Ба, 30 м2, вид на горы. Цена 6 млн/мес"


def test_the_publication_plan_keeps_the_earliest_copy() -> None:
    def row(external_id: str, url: str, posted: str) -> dict[str, Any]:
        return {
            "source": "archive",
            "external_id": external_id,
            "city": "nha_trang",
            "category": "apartment",
            "deal_type": "rent_out",
            "fetched_at": NOW,
            "url": url,
            "raw_text": TEXT,
            "posted_at": posted,
        }

    plan = primary_cleanup.publication_plan(
        [
            row("-1002:9", "https://t.me/b_group/9", "2026-11-10T00:00:00+00:00"),
            row("-1001:1", "https://t.me/a_group/1", "2026-11-01T00:00:00+00:00"),
        ]
    )

    by_key = {entry["key"]: entry for entry in plan}
    assert by_key[("archive", "-1001:1")]["duplicate_of"] is None
    assert by_key[("archive", "-1002:9")]["duplicate_of"] == ("archive", "-1001:1")
    assert all(entry["keys"] for entry in plan)


@pytest.mark.parametrize("near", [False, True])
def test_the_cleanup_never_writes_without_apply(
    monkeypatch: pytest.MonkeyPatch, near: bool
) -> None:
    applied = AsyncMock()
    monkeypatch.setattr(primary_cleanup, "apply", applied)
    monkeypatch.setattr(primary_cleanup, "apply_rebinds", applied)
    monkeypatch.setattr(primary_cleanup, "plan", AsyncMock(return_value=([], [], [], [])))

    class Repo:
        def __init__(self, session: object) -> None:
            pass

        async def earlier_copies(self) -> list[dict[str, Any]]:
            return []

        async def publications(self) -> list[dict[str, Any]]:
            return []

    @asynccontextmanager
    async def scope() -> AsyncIterator[object]:
        yield object()

    monkeypatch.setattr(primary_cleanup, "PrimaryCleanupRepository", Repo)
    monkeypatch.setattr(primary_cleanup, "session_scope", scope)

    planned = []
    real_plan = primary_cleanup.publication_plan

    def record_plan(rows: list[dict[str, Any]], *, near: bool = False) -> list[dict[str, Any]]:
        planned.append(near)
        return real_plan(rows, near=near)

    monkeypatch.setattr(primary_cleanup, "publication_plan", record_plan)
    assert primary_cleanup.main(["--near"] if near else []) == primary_cleanup.EXIT_OK
    assert planned == [near]
    applied.assert_not_awaited()


def test_an_unknown_failure_is_a_documented_code(monkeypatch: pytest.MonkeyPatch) -> None:
    class Boom(Exception):
        pass

    monkeypatch.setattr(primary_cleanup, "plan", AsyncMock(side_effect=Boom()))
    assert primary_cleanup.main([]) == primary_cleanup.EXIT_FAILURE

    monkeypatch.setattr(primary_cleanup, "plan", AsyncMock(side_effect=KeyboardInterrupt()))
    assert primary_cleanup.main([]) == primary_cleanup.EXIT_INTERRUPTED


def test_bad_arguments_exit_with_the_usage_code() -> None:
    with pytest.raises(SystemExit) as stop:
        primary_cleanup.main(["--samples", "1000"])
    assert stop.value.code == primary_cleanup.EXIT_USAGE


def test_publication_near_matching_requires_explicit_opt_in() -> None:
    from tests.test_listing_identity import FLAT

    base = {
        "source": "archive",
        "city": "nha_trang",
        "category": "apartment",
        "deal_type": "rent_out",
        "fetched_at": NOW,
        "posted_at": NOW.isoformat(),
    }
    rows = [
        {**base, "external_id": "1", "url": "https://t.me/a/1", "raw_text": FLAT},
        {
            **base,
            "external_id": "2",
            "url": "https://t.me/b/2",
            "raw_text": FLAT.replace("6 млн", "5 млн"),
        },
    ]
    exact = primary_cleanup.publication_plan(rows)
    assert all(row["duplicate_of"] is None for row in exact)
    assert all(not key.startswith("near:") for row in exact for key in row["keys"])
    near = primary_cleanup.publication_plan(rows, near=True)
    assert near[1]["duplicate_of"] == ("archive", "1")
