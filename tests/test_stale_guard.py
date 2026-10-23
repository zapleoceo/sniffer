"""Граница 30 суток: ворота воронки, обхода доски и CLI уборки. Без живой базы.

Тесты с базой — `test_stale_cleanup_db.py`. Здесь проверяется то, что не должно
доходить ни до базы, ни до модели.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from sniffer.db.repositories.listing_cleanup import StaleBatch, StaleOverview
from sniffer.domain.listing_state import LISTING_MAX_AGE_DAYS, is_stale, listing_cutoff
from sniffer.domain.records import RawMessage
from sniffer.worker import archive as archive_module
from sniffer.worker import stale_cleanup as cli
from sniffer.worker.chotot_sync import ChototSync
from tests.test_archive_reject_signals import _NoChat, _Recorder
from tests.test_chotot_sync import Board, Retired, Store

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=30)
SECOND = timedelta(seconds=1)


def test_the_single_constant_is_thirty_days() -> None:
    assert LISTING_MAX_AGE_DAYS == 30
    assert listing_cutoff(NOW) == CUTOFF


def test_boundary_is_strict_and_exact_to_the_second() -> None:
    assert is_stale(CUTOFF - SECOND, NOW) is True
    assert is_stale(CUTOFF, NOW) is False, "ровно на границе карточка жива, как в expire"
    assert is_stale(CUTOFF + SECOND, NOW) is False


def test_a_plus_seven_timestamp_is_compared_as_an_instant() -> None:
    hanoi = timezone(timedelta(hours=7))
    # Тот же момент, что CUTOFF - 1 с, записанный по вьетнамским часам.
    assert is_stale((CUTOFF - SECOND).astimezone(hanoi), NOW) is True
    assert is_stale((CUTOFF + SECOND).astimezone(hanoi), NOW) is False
    # Часы стены 19:00 в +07 — это 12:00 UTC, а не «позже» 12:00.
    assert is_stale(CUTOFF.astimezone(hanoi), NOW) is False


def test_naive_time_is_treated_as_utc() -> None:
    assert is_stale((CUTOFF - SECOND).replace(tzinfo=None), NOW) is True


# ── воронка архива ──────────────────────────────────────────────────────────


def raw(text: str, posted_at: datetime) -> RawMessage:
    return RawMessage(
        id=5, chat_tg_id=-100123, msg_id=1, text=text, text_hash="h", posted_at=posted_at
    )


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> type[_Recorder]:
    _Recorder.calls = []
    monkeypatch.setattr(archive_module, "RawMessageRepository", _Recorder)
    monkeypatch.setattr(archive_module, "ChatRepository", _NoChat)
    return _Recorder


async def test_a_stale_raw_is_marked_stale_before_the_gate_chat_or_any_model(
    recorder: type[_Recorder], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("стухшее сообщение не должно доходить до разбора")

    monkeypatch.setattr(archive_module, "classify", boom)
    monkeypatch.setattr(archive_module, "parse_query", boom)
    monkeypatch.setattr(archive_module, "listing_from", boom)
    text = "Продам Honda Vision 2021, цена 15.000.000 VND, документы есть"

    await archive_module.ArchivePipeline(now=lambda: NOW)._one(
        raw(text, CUTOFF - SECOND),
        None,  # type: ignore[arg-type]
    )

    assert recorder.calls == [{"ids": [5], "stage": "stale", "gate_signals": {"reason": "stale"}}]


async def test_a_message_on_the_boundary_still_goes_through_the_gate(
    recorder: type[_Recorder],
) -> None:
    text = "Продам Honda Vision 2021, цена 15.000.000 VND, документы есть"

    await archive_module.ArchivePipeline(now=lambda: NOW)._one(
        raw(text, CUTOFF),
        None,  # type: ignore[arg-type]
    )

    assert recorder.calls[0]["gate_signals"] == {"reason": "unknown_chat"}


# ── Chotot ──────────────────────────────────────────────────────────────────

# Фикстура доски датирована концом августа 2026.
BOARD_DAY = datetime(2026, 9, 1, tzinfo=UTC)


async def sync_at(now: datetime) -> Store:
    store = Store()
    job = ChototSync(
        interval_s=1800.0,
        source_factory=Board().source,
        store=store,
        retire=Retired(),
        clock=lambda: 0.0,
        now=lambda: now,
    )
    await job.tick()
    return store


async def test_board_items_older_than_thirty_days_never_become_cards() -> None:
    assert (await sync_at(BOARD_DAY)).rows, "контроль: на «свежую» дату фикстура даёт карточки"
    assert (await sync_at(BOARD_DAY + timedelta(days=60))).rows == []


async def test_board_retirement_still_sees_the_old_items_as_seen() -> None:
    retired = Retired()
    job = ChototSync(
        interval_s=1800.0,
        source_factory=Board().source,
        store=Store(),
        retire=retired,
        clock=lambda: 0.0,
        now=lambda: BOARD_DAY + timedelta(days=60),
    )
    await job.tick()

    assert retired.calls and all(seen for *_, seen in retired.calls)


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_default_is_a_dry_run() -> None:
    options = cli.parse_options([])
    assert options.execute is False


@pytest.mark.parametrize(
    "argv",
    [
        ["--execute", "--dry-run"],
        ["--confirm", "DELETE-STALE-2026-09-22"],
        ["--exec"],
        ["--batch-size", "0"],
        ["--batch-size", "abc"],
        ["--sleep-s", "-1"],
        ["--dry-run", "--dry-run"],
        ["--batch-size"],
        ["--max-batches", "0"],
        ["--max-batches", "x"],
        ["--max-batches", "1", "--max-batches", "2"],
        ["--execute", "--confirm", "DELETE-STALE-2026-09-22"],
    ],
)
def test_bad_arguments_are_a_usage_error(argv: list[str]) -> None:
    with pytest.raises(cli.UsageError):
        cli.parse_options(argv)


def test_execute_requires_an_explicit_bound_and_dry_run_does_not() -> None:
    assert cli.parse_options([]).max_batches is None
    assert cli.parse_options(["--max-batches=3"]).max_batches == 3
    with pytest.raises(cli.UsageError, match="max-batches"):
        cli.parse_options(["--execute", "--confirm", "DELETE-STALE-2026-09-22"])
    bounded = cli.parse_options(
        ["--execute", "--max-batches", "1", "--confirm", "DELETE-STALE-2026-09-22"]
    )
    assert (bounded.execute, bounded.max_batches) == (True, 1)


class FakeSession:
    async def commit(self) -> None:
        return None


@asynccontextmanager
async def fake_session() -> AsyncIterator[Any]:
    yield FakeSession()


class FakeRepo:
    """Три непустые пачки, затем пустая: проверяем только счётчик пачек."""

    calls = 0

    def __init__(self, _: Any) -> None: ...

    async def overview_stale(self, cutoff: datetime) -> StaleOverview:
        return StaleOverview(9, 0, 0, 0, 9, 0)

    async def delete_stale(
        self, cutoff: datetime, batch_size: int, dry_run: bool = True, *, after_id: int = 0
    ) -> StaleBatch:
        type(self).calls += 1
        if type(self).calls > 3:
            return StaleBatch(0, 0, 0, 0, 0, after_id)
        return StaleBatch(3, 0 if dry_run else 3, 0, 0, 0, after_id + 3)


async def test_max_batches_stops_the_pass_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "ListingRepository", FakeRepo)

    async def no_sleep(_: float) -> None:
        return None

    async def go(limit: int | None) -> cli.CleanupReport:
        FakeRepo.calls = 0
        return await cli.run_cleanup(
            cli.Options(max_batches=limit, sleep_s=0),
            cli.CleanupReport(),
            now=lambda: NOW,
            sleep=no_sleep,
            session_factory=fake_session,
        )

    capped = await go(2)
    assert (capped.batches, capped.selected, capped.limit_reached) == (2, 6, True)
    assert "--max-batches" in capped.render()
    assert FakeRepo.calls == 2, "после лимита в базу больше не ходим"
    full = await go(None)
    assert (full.batches, full.limit_reached) == (3, False)


def test_confirmation_string_is_bound_to_the_cutoff_date() -> None:
    assert cli.confirmation_for(CUTOFF) == "DELETE-STALE-2026-09-22"


@asynccontextmanager
async def no_database() -> AsyncIterator[Any]:
    raise AssertionError("до проверки подтверждения базу трогать нельзя")
    yield  # pragma: no cover


@pytest.mark.parametrize("confirm", [None, "", "yes", "DELETE-STALE-2026-09-21"])
async def test_execute_without_the_matching_confirmation_fails_before_any_query(
    confirm: str | None,
) -> None:
    options = cli.Options(execute=True, confirm=confirm)

    with pytest.raises(cli.UsageError):
        await cli.run_cleanup(
            options, cli.CleanupReport(), now=lambda: NOW, session_factory=no_database
        )


def test_the_exit_code_for_a_missing_confirmation_is_usage() -> None:
    errors: list[str] = []

    async def runner(options: cli.Options, report: cli.CleanupReport) -> cli.CleanupReport:
        return await cli.run_cleanup(options, report, now=lambda: NOW, session_factory=no_database)

    code = cli.run_stale_cleanup(
        ["--execute", "--max-batches", "1"], runner=runner, out=lambda _: None, err=errors.append
    )

    assert code == cli.EXIT_USAGE
    assert "DELETE-STALE-2026-09-22" in errors[0]


def test_the_subcommand_is_wired_into_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    from sniffer.worker import __main__ as worker_main

    received: list[list[str]] = []

    def fake(argv: Sequence[str]) -> int:
        received.append(list(argv))
        return 7

    monkeypatch.setattr(worker_main, "run_stale_cleanup", fake)
    monkeypatch.setattr(worker_main, "run_service", pytest.fail)

    assert worker_main.main(["stale-cleanup", "--dry-run"]) == 7
    assert received == [["--dry-run"]]
