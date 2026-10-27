"""CAS-замена hard_filter: что пишет UPDATE, аудит без PII, CLI отклоняет невалидное (без базы)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from structlog.testing import capture_logs

from sniffer.db.repositories.slots import (
    HardFilterUpdate,
    SlotRepository,
    audit_hard_filter_update,
)
from sniffer.domain.hard_filter import BALCONY, SEPARATE_KITCHEN, HardFilter
from sniffer.worker import update_hard_filter

OLD = HardFilter(
    require=frozenset({BALCONY, SEPARATE_KITCHEN}),
    districts=frozenset({"vinh_hoa"}),
    place_words=("duong de",),
)
NEW = HardFilter(
    require=OLD.require,
    districts=OLD.districts,
    place_words=OLD.place_words,
    allow_unknown=frozenset({BALCONY}),
)


class FakeSession:
    """Запоминает UPDATE и отвечает «строка обновлена» либо «не совпало»."""

    def __init__(self, updated: bool) -> None:
        self.updated = updated
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        row = SimpleNamespace(id=5, hard_filter=OLD.to_json())
        return SimpleNamespace(
            scalar_one_or_none=lambda: 5 if self.updated else None,
            one_or_none=lambda: row,
        )


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]


def _repo(session: FakeSession) -> SlotRepository:
    return SlotRepository(session)  # type: ignore[arg-type]


async def test_the_update_sets_only_the_hard_filter_column_and_compares_the_old_value() -> None:
    session = FakeSession(updated=True)

    result = await _repo(session).update_hard_filter(5, expected=OLD, new=NEW)

    sql = _sql(session.statements[0])
    set_clause = sql.split("SET", 1)[1].split("WHERE", 1)[0]
    assert set_clause.strip().startswith("hard_filter")
    for column in ("is_active", "expires_at", "no_slot_since", "listing_id"):
        assert column not in set_clause
    assert "subscriptions.hard_filter =" in sql and "subscriptions.id =" in sql
    assert result.applied and result.current == NEW.to_json()


async def test_a_conflict_returns_the_current_value_and_does_not_log_the_content() -> None:
    session = FakeSession(updated=False)

    with capture_logs() as logs:
        result = await _repo(session).update_hard_filter(5, expected=None, new=NEW)

    assert not result.applied and result.current == OLD.to_json()
    assert "IS NULL" in _sql(session.statements[0])
    assert [entry["event"] for entry in logs] == ["subscription.hard_filter_conflict"]


async def test_the_update_itself_writes_no_success_audit_because_the_commit_is_not_done() -> None:
    with capture_logs() as logs:
        result = await _repo(FakeSession(updated=True)).update_hard_filter(5, expected=OLD, new=NEW)

    assert result.applied
    assert logs == [], "аудит успеха — только после commit и чтения (CLI)"


def test_the_audit_has_ids_and_digests_only_no_filter_content() -> None:
    with capture_logs() as logs:
        audit_hard_filter_update(5, before=OLD.to_json(), after=NEW.to_json())

    (entry,) = logs
    assert entry["event"] == "subscription.hard_filter_updated"
    assert set(entry) == {"event", "log_level", "subscription", "before", "after"}
    dump = str(entry)
    for word in ("vinh_hoa", "duong de", "balcony", "districts"):
        assert word not in dump
    assert entry["before"] != entry["after"]


@pytest.mark.parametrize(
    ("expect", "new"),
    [
        ("{", '{"require": ["balcony"]}'),
        ('{"require": ["balcony"]}', "null"),
        ('{"require": ["balcony"]}', "[]"),
        ('{"require": ["balcon"]}', '{"require": ["balcony"]}'),
        ('{"require": ["balcony"]}', '{"require": ["balcony"], "allow_unknown": null}'),
        ('{"require": ["balcony"]}', '{"radius_m": 5}'),
    ],
)
def test_invalid_json_is_rejected_before_the_database(expect: str, new: str) -> None:
    args = update_hard_filter.parse(
        ["--subscription-id", "1", "--expect-json", expect, "--set-json", new]
    )
    with pytest.raises(ValueError):
        update_hard_filter.build(args)


def test_the_cli_reports_invalid_input_as_usage_not_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = update_hard_filter.main(
        ["--subscription-id", "1", "--expect-json", "null", "--set-json", '{"require": ["x"]}']
    )

    assert code == update_hard_filter.EXIT_USAGE
    assert "hard_filter" in capsys.readouterr().err


def test_null_expected_means_no_filter_and_a_valid_pair_parses() -> None:
    args = update_hard_filter.parse(
        [
            "--subscription-id",
            "1",
            "--expect-json",
            "null",
            "--set-json",
            '{"require": ["balcony"], "allow_unknown": ["balcony"]}',
        ]
    )
    expected, new = update_hard_filter.build(args)
    assert expected is None
    assert new.allow_unknown == frozenset({BALCONY})


class Journal:
    """Порядок событий CLI: что произошло раньше — commit, чтение или аудит."""

    def __init__(self) -> None:
        self.events: list[str] = []


class CliSession:
    def __init__(self, journal: Journal, commit_error: BaseException | None) -> None:
        self.journal = journal
        self.commit_error = commit_error

    async def commit(self) -> None:
        self.journal.events.append("commit")
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self) -> None:
        self.journal.events.append("rollback")


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    applied: bool = True,
    commit_error: BaseException | None = None,
    update_error: BaseException | None = None,
    readback: dict[str, Any] | None = None,
) -> Journal:
    journal = Journal()

    @asynccontextmanager
    async def scope() -> Any:
        yield CliSession(journal, commit_error)

    class FakeRepo:
        def __init__(self, session: Any) -> None:
            pass

        async def update_hard_filter(self, *args: Any, **kwargs: Any) -> Any:
            journal.events.append("update")
            if update_error is not None:
                raise update_error
            return HardFilterUpdate(applied=applied, current=OLD.to_json())

        async def read_hard_filter(self, subscription_id: int) -> Any:
            journal.events.append("read")
            return readback if readback is not None else NEW.to_json()

    def audit(*args: Any, **kwargs: Any) -> None:
        journal.events.append("audit")

    monkeypatch.setattr(update_hard_filter, "session_scope", scope)
    monkeypatch.setattr(update_hard_filter, "SlotRepository", FakeRepo)
    monkeypatch.setattr(update_hard_filter, "audit_hard_filter_update", audit)
    return journal


def _argv() -> list[str]:
    import json

    return [
        "--subscription-id",
        "5",
        "--expect-json",
        json.dumps(OLD.to_json()),
        "--set-json",
        json.dumps(NEW.to_json()),
    ]


def test_success_is_audited_only_after_commit_and_the_stdout_is_a_real_readback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = _wire(monkeypatch)

    assert update_hard_filter.main(_argv()) == update_hard_filter.EXIT_OK

    assert journal.events == ["update", "commit", "read", "audit"]
    assert "updated subscription=5 readback=" in capsys.readouterr().out


def test_a_readback_that_differs_is_not_reported_as_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = _wire(monkeypatch, readback=OLD.to_json())

    assert update_hard_filter.main(_argv()) == update_hard_filter.EXIT_UNVERIFIED

    assert "audit" not in journal.events
    assert "updated subscription" not in capsys.readouterr().out


class Boom(Exception):
    pass


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (Boom("secret-token-123"), update_hard_filter.EXIT_FAILURE),
        (KeyboardInterrupt(), update_hard_filter.EXIT_INTERRUPTED),
        (asyncio.CancelledError(), update_hard_filter.EXIT_INTERRUPTED),
        (
            BaseExceptionGroup("g", [asyncio.CancelledError(), KeyboardInterrupt()]),
            update_hard_filter.EXIT_INTERRUPTED,
        ),
        (
            BaseExceptionGroup("g", [asyncio.CancelledError(), Boom("secret-token-123")]),
            update_hard_filter.EXIT_FAILURE,
        ),
    ],
)
@pytest.mark.parametrize("where", ["commit", "update"])
def test_a_failed_or_interrupted_step_gives_a_closed_code_and_no_false_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: BaseException,
    code: int,
    where: str,
) -> None:
    journal = _wire(
        monkeypatch,
        commit_error=error if where == "commit" else None,
        update_error=error if where == "update" else None,
    )

    assert update_hard_filter.main(_argv()) == code

    captured = capsys.readouterr()
    assert "audit" not in journal.events and "read" not in journal.events
    assert "updated" not in captured.out
    assert "secret-token-123" not in captured.err + captured.out
    assert "Traceback" not in captured.err


def test_a_conflict_rolls_back_and_writes_no_success_audit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = _wire(monkeypatch, applied=False)

    assert update_hard_filter.main(_argv()) == update_hard_filter.EXIT_CONFLICT

    assert journal.events == ["update", "rollback"]
    assert "conflict subscription=5" in capsys.readouterr().out


def test_a_foreign_exit_request_is_not_rewritten_into_our_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(monkeypatch, commit_error=GeneratorExit())

    with pytest.raises(GeneratorExit):
        update_hard_filter.main(_argv())


def test_an_unexpected_error_outside_the_database_step_is_also_a_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(argv: object) -> None:
        raise Boom("secret-token-123")

    monkeypatch.setattr(update_hard_filter, "parse", boom)

    assert update_hard_filter.main([]) == update_hard_filter.EXIT_FAILURE
