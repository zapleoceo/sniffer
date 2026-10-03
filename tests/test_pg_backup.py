"""Резервная копия БД: `infra/backup/sniffer-pg-backup.sh` запускается настоящим bash.

У БД sniffer резервной копии не было вовсе, а в неё ложатся платежи и права.
Копия, которой нельзя доверять, хуже отсутствующей: на неё положатся. Поэтому
проверяется поведение, а не вид строк: скрипт идёт против подставного `docker`,
который отдаёт «дамп» с узнаваемыми строками.

* копия лежит под именем с временем, с правами 0600, и читается;
* пустой или слишком маленький дамп, упавший `pg_dump` и мёртвый `docker` дают
  код возврата != 0, а недописанного файла не остаётся;
* старые копии удаляются ТОЛЬКО после хорошей и только свои (`sniffer-*.sql.gz`);
* в журнал не попадает ни строка дампа, ни пароль.
"""

from __future__ import annotations

import gzip
import os
import re
import time
from pathlib import Path

import pytest

from tests.shell_support import TRACE_CHMOD, Ran, needs_bash, run
from tests.sql_chain_support import ROOT

SCRIPT = ROOT / "infra" / "backup" / "sniffer-pg-backup.sh"
COPY = re.compile(r"sniffer-\d{8}-\d{4}\.sql\.gz")
HOUR = 3600

# Подставной `docker` обязан получить ровно эту команду: чужое имя контейнера,
# лишний флаг (`-t` портит дамп переводами строк) или другая база — это отказ.
EXPECTED_CALL = "exec sniffer-postgres pg_dump -U sniffer -d sniffer"
DUMP = 'seq 1 4000 | sed "s/.*/INSERT INTO listings VALUES (&, SECRETROW&);/"'
MODES = {
    "ok": DUMP,
    "empty": "true",
    "tiny": "echo '-- tiny'",
    "fail": "echo 'No such container' >&2; return 1",
    "fail_after_output": f"{DUMP}; return 1",
}


def _fake_docker(mode: str) -> str:
    return "\n".join(
        [
            "docker() {",
            f'  if [ "$*" != "{EXPECTED_CALL}" ]; then',
            '    echo "неожиданный вызов docker: $*" >&2; return 99',
            "  fi",
            f"  {MODES[mode]}",
            "}",
            "export -f docker",
        ]
    )


def _run_script(backups: Path, mode: str = "ok", env: str = "") -> Ran:
    """Запустить скрипт; код возврата — его собственный (`exec`), а не обёртки."""
    program = "\n".join(
        [
            _fake_docker(mode),
            f'export BACKUP_DIR="{backups.as_posix()}"',
            env,
            f'exec bash "{SCRIPT.as_posix()}"',
        ]
    )
    done = run(program)
    assert done is not None
    return done


def _copies(backups: Path) -> list[Path]:
    return sorted(path for path in backups.glob("*") if COPY.fullmatch(path.name))


def _age(path: Path, hours: int) -> Path:
    stamp = time.time() - hours * HOUR
    os.utime(path, (stamp, stamp))
    return path


def _mode(path: Path) -> str:
    """Права файла так, как их видит bash: Python на Windows отдаёт не POSIX-биты."""
    done = run(f'stat -c %a "{path.as_posix()}"')
    assert done is not None
    return done.stdout.strip()


def _modes_are_real() -> bool:
    """Хранит ли файловая система права: NTFS под MSYS (mount noacl) их не помнит."""
    probe = "\n".join(
        [
            'f="$(mktemp)"',
            'chmod 600 "$f"',
            """bash -c 'umask 022; stat -c %a "$0"' "$f" """,
            'rm -f "$f"',
        ]
    )
    done = run(probe)
    return done is not None and done.stdout.strip() == "600"


MODES_ARE_REAL = _modes_are_real()


# ── сам скрипт: вид, который нельзя проверить запуском ───────────────────────


def test_the_script_is_strict_private_and_has_unix_line_endings() -> None:
    text = SCRIPT.read_bytes().decode("utf-8")

    assert "set -euo pipefail" in text
    assert "umask 077" in text
    assert 'chmod 600 "$part"' in text
    assert 'chmod 700 "$BACKUP_DIR"' in text
    assert chr(13) not in text, "CRLF в скрипте: Linux не найдёт интерпретатор"


def test_the_script_carries_no_password() -> None:
    """`pg_dump` внутри контейнера идёт по сокету под `sniffer`: пароль скрипту не нужен."""
    text = SCRIPT.read_text(encoding="utf-8")

    for forbidden in ("-- password", "--password", "PGPASSWORD", "POSTGRES_PASSWORD", "-W "):
        assert forbidden not in text, f"в скрипте резервной копии есть {forbidden!r}"


@needs_bash
def test_the_script_parses() -> None:
    done = run(f'bash -n "{SCRIPT.as_posix()}"')

    assert done is not None and done.code == 0, done and done.text


# ── хорошая копия ────────────────────────────────────────────────────────────


@needs_bash
def test_a_run_writes_a_private_readable_archive_named_by_the_clock(tmp_path: Path) -> None:
    done = _run_script(tmp_path / "backups")

    (copy,) = _copies(tmp_path / "backups")
    assert done.code == 0, done.text
    assert "SECRETROW4000" in gzip.decompress(copy.read_bytes()).decode("utf-8")
    assert not list((tmp_path / "backups").glob("*.part*")), "после копии остался недописанный файл"
    assert copy.name in done.stdout


@needs_bash
def test_the_dump_never_reaches_the_log(tmp_path: Path) -> None:
    done = _run_script(tmp_path / "backups")

    assert done.code == 0, done.text
    assert "SECRETROW" not in done.text
    assert "INSERT" not in done.text
    assert len(done.text.splitlines()) <= 3, "журнал копии — строка-две статуса, а не поток"


@needs_bash
@pytest.mark.skipif(not MODES_ARE_REAL, reason="файловая система не хранит права (NTFS под MSYS)")
def test_the_archive_and_the_directory_are_private(tmp_path: Path) -> None:
    done = _run_script(tmp_path / "backups")

    (copy,) = _copies(tmp_path / "backups")
    assert done.code == 0, done.text
    assert _mode(copy) == "600"
    assert _mode(tmp_path / "backups") == "700"


@needs_bash
def test_the_script_asks_for_private_modes(tmp_path: Path) -> None:
    """То же на любой файловой системе: что скрипт попросил, а не что она сохранила."""
    backups, trace = tmp_path / "backups", tmp_path / "trace.txt"

    done = _run_script(backups, env=f'export TRACE="{trace.as_posix()}"; {TRACE_CHMOD}')

    asked = trace.read_text(encoding="utf-8").splitlines()
    archive = rf"600 {re.escape(backups.as_posix())}/{COPY.pattern}\.part\.\d+"
    assert done.code == 0, done.text
    assert f"700 {backups.as_posix()}" in asked
    assert any(re.fullmatch(archive, line) for line in asked), asked


# ── плохая копия не притворяется хорошей ─────────────────────────────────────


@needs_bash
@pytest.mark.parametrize("mode", ["empty", "tiny"])
def test_a_dump_smaller_than_the_minimum_fails_and_leaves_no_copy(
    tmp_path: Path, mode: str
) -> None:
    """Пустой дамп при нулевом коде `pg_dump` бывает, и на копию из него положатся."""
    done = _run_script(tmp_path / "backups", mode)

    assert done.code == 2, done.text
    assert _copies(tmp_path / "backups") == []
    assert not list((tmp_path / "backups").glob("*.part*"))


@needs_bash
def test_the_size_threshold_is_exact(tmp_path: Path) -> None:
    """Ровно минимум проходит, на байт меньше — нет: `-lt`, а не `-le`."""
    probe = tmp_path / "probe"
    assert _run_script(probe, env="export MIN_BYTES=1").code == 0
    size = _copies(probe)[0].stat().st_size

    at_minimum = _run_script(tmp_path / "at", env=f"export MIN_BYTES={size}")
    below = _run_script(tmp_path / "below", env=f"export MIN_BYTES={size + 1}")

    assert at_minimum.code == 0, at_minimum.text
    assert below.code == 2, below.text


@needs_bash
@pytest.mark.parametrize("mode", ["fail", "fail_after_output"])
def test_a_failing_pg_dump_fails_even_if_it_printed_something(tmp_path: Path, mode: str) -> None:
    """Без `pipefail` код возврата конвейера — это код `gzip`, то есть 0."""
    done = _run_script(tmp_path / "backups", mode)

    assert done.code == 1, done.text
    assert _copies(tmp_path / "backups") == []
    assert not list((tmp_path / "backups").glob("*.part*"))


@needs_bash
def test_an_archive_that_fails_gzip_test_is_discarded(tmp_path: Path) -> None:
    """Размер хорош, а архив не читается: такая копия хуже отсутствующей."""
    broken = 'gzip() { if [ "$1" = "-t" ]; then return 1; fi; command gzip "$@"; }; export -f gzip'

    done = _run_script(tmp_path / "backups", env=broken)

    assert done.code == 3, done.text
    assert _copies(tmp_path / "backups") == []
    assert not list((tmp_path / "backups").glob("*.part*"))


@needs_bash
def test_a_wrong_docker_call_is_a_failure(tmp_path: Path) -> None:
    done = _run_script(tmp_path / "backups", env='export PG_CONTAINER="not-ours"')

    assert done.code != 0
    assert _copies(tmp_path / "backups") == []


# ── порядок: старые копии уходят только после хорошей ────────────────────────


def _seed(backups: Path, name: str, hours: int) -> Path:
    backups.mkdir(exist_ok=True)
    path = backups / name
    path.write_bytes(b"x")
    return _age(path, hours)


@needs_bash
def test_old_copies_go_after_a_good_run_and_only_their_own(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    old = _seed(backups, "sniffer-20200101-0320.sql.gz", 73)
    young = _seed(backups, "sniffer-20200102-0320.sql.gz", 71)
    notes = _seed(backups, "notes.txt", 100)
    plain = _seed(backups, "sniffer-20200101-0320.sql", 100)
    stale_part = _seed(backups, "sniffer-20200103-0320.sql.gz.part.123", 30)
    live_part = _seed(backups, "sniffer-20200104-0320.sql.gz.part.456", 1)

    done = _run_script(backups)

    assert done.code == 0, done.text
    assert not old.exists(), "копия старше трёх суток не удалена"
    assert not stale_part.exists(), "брошенный недописанный файл не убран"
    assert young.exists() and notes.exists() and plain.exists() and live_part.exists()
    assert len(_copies(backups)) == 2
    assert "удалено старых: 1" in done.stdout


@needs_bash
def test_a_failed_run_keeps_every_old_copy(tmp_path: Path) -> None:
    """Сбойный запуск не вправе стирать последнюю хорошую копию."""
    backups = tmp_path / "backups"
    old = _seed(backups, "sniffer-20200101-0320.sql.gz", 200)

    done = _run_script(backups, "fail")

    assert done.code == 1, done.text
    assert old.exists()


@needs_bash
def test_the_retention_follows_keep_days(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    gone = _seed(backups, "sniffer-20200101-0320.sql.gz", 25)
    kept = _seed(backups, "sniffer-20200102-0320.sql.gz", 23)

    done = _run_script(backups, env="export KEEP_DAYS=1")

    assert done.code == 0, done.text
    assert not gone.exists() and kept.exists()


@needs_bash
@pytest.mark.parametrize(
    ("name", "value"),
    [("KEEP_DAYS", "0"), ("KEEP_DAYS", "abc"), ("KEEP_DAYS", "-1"), ("MIN_BYTES", "abc")],
)
def test_a_bad_setting_stops_the_run_before_anything_is_touched(
    tmp_path: Path, name: str, value: str
) -> None:
    """`KEEP_DAYS=0` стёр бы и свежую копию, поэтому не пускается вовсе."""
    done = _run_script(tmp_path / "backups", env=f'export {name}="{value}"')

    assert done.code == 4, done.text
    assert not (tmp_path / "backups").exists()
