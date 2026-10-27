"""CAS-замена hard_filter: что пишет UPDATE, аудит без PII, CLI отклоняет невалидное (без базы)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from structlog.testing import capture_logs

from sniffer.db.repositories.slots import SlotRepository
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


async def test_the_audit_has_ids_and_digests_only_no_filter_content() -> None:
    with capture_logs() as logs:
        await _repo(FakeSession(updated=True)).update_hard_filter(5, expected=OLD, new=NEW)

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
