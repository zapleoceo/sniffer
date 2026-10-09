"""CLI `chat_exclusion`: коды возврата, dry-run, идемпотентность и охрана до корня иерархии.

База здесь подменена фейками: что команда пишет в транзакции, проверяют тесты на Postgres
(`test_chat_exclusion_db.py`), а здесь — что она делает вокруг: что читает, когда коммитит,
чем отвечает на любое исключение. Telegram команде не нужен вовсе, и тест это сторожит.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from sniffer.db.repositories import chat_exclusion as repository
from sniffer.domain.records import Chat
from sniffer.worker import chat_exclusion as cli
from sniffer.worker.chat_exclusion import (
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_NOOP,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
)

EVIDENCE = json.dumps({"messages": 1840, "useful_offers": 0, "backfill_done": True})
EXCLUDE = ["exclude", "--tg-id", "-1001", "--reason", "zero_useful", "--evidence-json", EVIDENCE]
RESTORE = ["restore", "--tg-id", "-1001", "--reason", "ошиблись"]


@dataclass
class Disk:
    """«Диск»: чаты, журнал и число коммитов."""

    chats: dict[int, Chat] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    commits: int = 0


class FakeSession:
    def __init__(self, disk: Disk) -> None:
        self.disk = disk

    async def commit(self) -> None:
        self.disk.commits += 1


def install(monkeypatch: pytest.MonkeyPatch, disk: Disk) -> None:
    @asynccontextmanager
    async def scope() -> AsyncIterator[FakeSession]:
        yield FakeSession(disk)

    class Chats:
        def __init__(self, session: FakeSession) -> None:
            self.disk = session.disk

        async def get_by_tg_id(self, tg_id: int) -> Chat | None:
            return self.disk.chats.get(tg_id)

    class Exclusion:
        def __init__(self, session: FakeSession) -> None:
            self.disk = session.disk

        async def exclude(self, tg_id: int, **kwargs: Any) -> repository.Outcome:
            self.disk.events.append({"action": "exclude", "tg_id": tg_id, **kwargs})
            return repository.Outcome.DONE

        async def restore(self, tg_id: int, **kwargs: Any) -> repository.Outcome:
            self.disk.events.append({"action": "restore", "tg_id": tg_id, **kwargs})
            return repository.Outcome.DONE

    monkeypatch.setattr(cli, "session_scope", scope)
    monkeypatch.setattr(cli, "ChatRepository", Chats)
    monkeypatch.setattr(cli, "ChatExclusionRepository", Exclusion)


def chat(*, excluded: bool = False) -> Chat:
    from datetime import UTC, datetime

    return Chat(
        tg_id=-1001,
        title="Мёртвая барахолка",
        city="nha_trang",
        excluded_at=datetime.now(UTC) if excluded else None,
    )


def test_exclude_writes_one_event_and_commits_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    disk = Disk(chats={-1001: chat()})
    install(monkeypatch, disk)

    assert cli.main(EXCLUDE) == EXIT_OK

    assert [e["action"] for e in disk.events] == ["exclude"]
    assert disk.events[0]["reason"] == "zero_useful"
    assert disk.events[0]["evidence"]["useful_offers"] == 0
    assert "snapshot_at" in disk.events[0]["evidence"]  # дата снимка дописана сама
    assert disk.commits == 1
    assert "exclude выполнен" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [EXCLUDE, RESTORE])
def test_dry_run_reads_but_never_writes_or_commits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    disk = Disk(chats={-1001: chat(excluded=argv is RESTORE)})
    install(monkeypatch, disk)

    assert cli.main([*argv, "--dry-run"]) == EXIT_OK

    assert disk.events == [] and disk.commits == 0
    assert "dry-run" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv", "already_excluded"), [(EXCLUDE, True), (RESTORE, False)], ids=["exclude", "restore"]
)
def test_a_repeat_is_a_noop_with_its_own_code_and_no_second_event(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    already_excluded: bool,
) -> None:
    disk = Disk(chats={-1001: chat(excluded=already_excluded)})
    install(monkeypatch, disk)

    assert cli.main(argv) == EXIT_NOOP

    assert disk.events == [] and disk.commits == 0
    assert "ничего не меняю" in capsys.readouterr().out


def test_an_unknown_chat_is_code_3(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, Disk())
    assert cli.main(EXCLUDE) == EXIT_NOT_FOUND
    assert cli.main(RESTORE) == EXIT_NOT_FOUND


@pytest.mark.parametrize("evidence", ["не json", "[]", "{}", '"строка"', "123"])
def test_a_bad_evidence_is_a_usage_error_before_the_database_is_touched(
    monkeypatch: pytest.MonkeyPatch, evidence: str
) -> None:
    disk = Disk(chats={-1001: chat()})
    install(monkeypatch, disk)
    argv = ["exclude", "--tg-id", "-1001", "--reason", "r", "--evidence-json", evidence]

    assert cli.main(argv) == EXIT_USAGE

    assert disk.events == [] and disk.commits == 0


@pytest.mark.parametrize(
    "argv",
    [[], ["exclude"], ["exclude", "--tg-id", "1"], ["restore", "--tg-id", "x", "--reason", "r"]],
)
def test_wrong_arguments_exit_with_argparses_code_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        cli.main(argv)
    assert stop.value.code == EXIT_USAGE


# ── охрана: корень иерархии, обе оси ────────────────────────────────────────────────────


class Boom(Exception):
    """Тип, о котором код не знает."""


def failing(error: BaseException) -> Any:
    async def execute(*_args: object, **_kwargs: object) -> int:
        raise error

    return execute


def test_an_unknown_exception_is_a_documented_code_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "execute", failing(Boom("postgresql://user:secret@host/db")))

    assert cli.main(EXCLUDE) == EXIT_FAILURE

    err = capsys.readouterr().err
    assert "Boom" in err and "secret" not in err  # тип назван, текст драйвера не выносим


@pytest.mark.parametrize(
    "error",
    [
        KeyboardInterrupt(),
        asyncio.CancelledError(),
        BaseExceptionGroup("tg", [KeyboardInterrupt(), asyncio.CancelledError()]),
    ],
    ids=["ctrl-c", "cancelled", "group-of-interrupts"],
)
def test_an_interrupt_is_words_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: BaseException
) -> None:
    monkeypatch.setattr(cli, "execute", failing(error))

    assert cli.main(EXCLUDE) == EXIT_INTERRUPTED
    assert "ничего не записано" in capsys.readouterr().err


def test_a_mixed_group_is_a_failure_not_an_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    group = BaseExceptionGroup("tg", [asyncio.CancelledError(), Boom("настоящий сбой")])
    monkeypatch.setattr(cli, "execute", failing(group))

    assert cli.main(EXCLUDE) == EXIT_FAILURE


@pytest.mark.parametrize("error", [SystemExit(7), GeneratorExit()], ids=["exit", "generator"])
def test_a_request_to_stop_is_passed_through_not_rewritten(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    monkeypatch.setattr(cli, "execute", failing(error))

    with pytest.raises(type(error)):
        cli.main(EXCLUDE)


def test_the_guard_reaches_the_root_of_the_hierarchy() -> None:
    """Устройство, а не список: последний `except` — корень, и `except Exception` нет вовсе."""
    source = inspect.getsource(cli.main)
    assert len(re.findall(r"except BaseException", source)) == 1
    assert "except Exception" not in inspect.getsource(cli)
    assert source.rstrip().splitlines()[-1].strip().startswith("return EXIT_FAILURE")


def test_the_command_never_talks_to_telegram() -> None:
    """Ни Telethon, ни выхода из группы: исключение — запись в базе."""
    for module in (cli, repository):
        text = inspect.getsource(module)
        assert "telethon" not in text.lower()
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert not re.search(r"LeaveChannel|leave_chat|DeleteChat|kick_participant|\.send_", code)


def test_parse_accepts_the_documented_forms() -> None:
    args = cli.parse(["restore", "--tg-id", "5", "--reason", "x", "--dry-run", "--actor", "me"])
    assert (args.action, args.tg_id, args.dry_run, args.actor) == ("restore", 5, True, "me")
    assert cli.parse(EXCLUDE).dry_run is False


def test_evidence_keeps_the_callers_snapshot_date() -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    kept = cli.evidence_from('{"snapshot_at": "2026-10-09", "messages": 1}', now)
    assert kept["snapshot_at"] == "2026-10-09"
    assert cli.evidence_from('{"messages": 1}', now)["snapshot_at"] == now.isoformat()


def test_interrupt_detection_is_one_function_over_both_axes() -> None:
    assert cli.is_interrupt(KeyboardInterrupt()) and cli.is_interrupt(asyncio.CancelledError())
    assert not cli.is_interrupt(Boom())
    assert not cli.is_interrupt(BaseExceptionGroup("x", [Boom()]))
