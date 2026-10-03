"""Маска миграций: файл `010_*.sql` не должен пропадать молча.

Деплой, CI и тесты брали файлы цепочки по маске из двух нулей, поэтому первый
же файл с номером 010 не применился бы нигде — ни на сервере, ни в тестовой
базе, ни в тесте схемы, — а сборка осталась бы зелёной: пропущенного файла
никто не видит. Маска теперь — трёхзначный номер, и держится это не
договорённостью, а проверками:

* маска из `deploy.sh` и `quality.yml` вынимается из ТЕКСТА и совпадает с той,
  что у тестов (`tests.sql_chain_support.MASK`);
* она подходит к 009, 010 и 123 и не подходит к `readme.sql` и к номерам не из
  трёх цифр;
* настоящий bash раскрывает её в тот же набор и в том же порядке, что и Python;
* каждый `.sql` в `infra/sql` под неё подпадает: иначе файл применять некому;
* старой маски нет ни в одном файле репозитория, включая документацию.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest

from tests.shell_support import needs_bash, run
from tests.sql_chain_support import MASK, ROOT, SQL_DIR, chain

DEPLOY = ROOT / "infra" / "deploy.sh"
QUALITY = ROOT / ".github" / "workflows" / "quality.yml"
CONSUMERS = [pytest.param(DEPLOY, id="deploy.sh"), pytest.param(QUALITY, id="quality.yml")]

# Цикл по миграциям: одна строка, отступ допустим (в workflow она вложена).
_LOOP = re.compile(r"^[ \t]*for migration in (\S+); do$", re.MULTILINE)

TAKEN = ["001_init.sql", "009_x.sql", "010_x.sql", "099_x.sql", "123_x.sql", "999_x.sql"]
SKIPPED = [
    "readme.sql",
    "10_x.sql",
    "0010_x.sql",
    "010.sql",
    "010_x.sql.bak",
    "x010_x.sql",
    "010_x.txt",
]

# Старая маска записана кусками: целиком она нашла бы сам этот файл.
OLD_MASK = "00" + "*.sql"
SCANNED = ("infra", ".github", "tests", "docs", "src")
TEXT_SUFFIXES = {".py", ".sh", ".yml", ".yaml", ".md", ".sql", ".toml", ".txt"}


def _mask_of(path: Path) -> str:
    found: list[str] = _LOOP.findall(path.read_text(encoding="utf-8"))
    assert len(found) == 1, f"{path.name}: ждали ровно один цикл по миграциям, нашли {len(found)}"
    assert found[0].startswith("infra/sql/"), f"{path.name}: цикл идёт не по infra/sql"
    return found[0].removeprefix("infra/sql/")


@pytest.mark.parametrize("path", CONSUMERS)
def test_the_consumer_takes_the_mask_the_tests_take(path: Path) -> None:
    assert _mask_of(path) == MASK


@pytest.mark.parametrize("path", CONSUMERS)
def test_the_mask_takes_every_number_and_nothing_else(path: Path) -> None:
    mask = _mask_of(path)

    missed = [name for name in TAKEN if not fnmatch.fnmatchcase(name, mask)]
    caught = [name for name in SKIPPED if fnmatch.fnmatchcase(name, mask)]

    assert not missed, f"маска {mask} не берёт файлы цепочки: {missed}"
    assert not caught, f"маска {mask} берёт то, что цепочкой не является: {caught}"


@needs_bash
@pytest.mark.parametrize("path", CONSUMERS)
def test_bash_expands_the_mask_in_alphabetical_order(path: Path) -> None:
    """Не только Python-сопоставление имён: цикл исполняется тем bash, что на сервере."""
    names = [*TAKEN, *SKIPPED, "002_agent.sql", "002_seed.sql"]
    program = "\n".join(
        [
            'root="$(mktemp -d)"',
            'mkdir -p "$root/infra/sql" && cd "$root"',
            *[f': > "infra/sql/{name}"' for name in names],
            f'for migration in infra/sql/{_mask_of(path)}; do echo "$migration"; done',
            'cd / && rm -rf "$root"',
        ]
    )
    expected = [f"infra/sql/{n}" for n in sorted(n for n in names if fnmatch.fnmatchcase(n, MASK))]

    done = run(program)

    assert done is not None and done.code == 0, done and done.text
    assert done.stdout.split() == expected


def test_every_sql_file_in_the_directory_belongs_to_the_chain() -> None:
    """Файл вне маски (`10_x.sql`, `quota.sql`) не применится нигде, и никто этого не заметит."""
    taken = set(chain())
    outside = sorted(path.name for path in SQL_DIR.glob("*.sql") if path not in taken)

    assert not outside, (
        f"файлы вне маски {MASK}: {outside}. Деплой, CI и тесты их не применят — "
        "назовите файл `NNN_имя.sql` с трёхзначным номером"
    )


def test_the_chain_sees_a_file_numbered_010(tmp_path: Path) -> None:
    for name in ("001_a.sql", "010_probe.sql", "readme.sql"):
        (tmp_path / name).write_text("-- probe\n", encoding="utf-8")

    assert [path.name for path in chain(tmp_path)] == ["001_a.sql", "010_probe.sql"]


def test_the_old_mask_is_gone_from_the_repository() -> None:
    """Возврат к старой маске ломает сборку, где бы он ни случился: в коде, CI или документации."""
    files = list(ROOT.glob("*.md"))
    for base in SCANNED:
        files += [
            path
            for path in (ROOT / base).rglob("*")
            if path.is_file() and path.suffix in TEXT_SUFFIXES and "__pycache__" not in path.parts
        ]
    offenders = [
        f"{path.relative_to(ROOT).as_posix()}:{number}"
        for path in files
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1
        )
        if OLD_MASK in line
    ]

    assert files, "ни одного файла для проверки: сканируется не то"
    assert not offenders, f"старая маска из двух нулей: {offenders}"
