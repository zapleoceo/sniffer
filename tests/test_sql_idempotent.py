"""Каждый файл цепочки миграций переживает повторный прогон: деплой гонит его на КАЖДОМ запуске.

`CREATE TABLE` без `IF NOT EXISTS` падает со второго деплоя, а `ALTER … ADD COLUMN`
без него — тоже; падение же миграции красит деплой кодом 40 на пустом месте. Поведение
на живой базе проверяет `test_sql_chain_db.py` (в CI), а эта проверка статическая и идёт
везде: забытое слово видно до пуша, а не на втором деплое.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SQL_DIR = Path(__file__).resolve().parents[1] / "infra" / "sql"
CHAIN = sorted(SQL_DIR.glob("[0-9][0-9][0-9]_*.sql"))

CREATE = re.compile(
    r"^[ \t]*CREATE[ \t]+(?:UNIQUE[ \t]+)?(?:TABLE|INDEX|SEQUENCE|VIEW|EXTENSION)\b([^;(]*)",
    re.IGNORECASE | re.MULTILINE,
)
ADD_COLUMN = re.compile(
    r"ADD[ \t\r\n]+COLUMN[ \t\r\n]+(?!IF[ \t\r\n]+NOT[ \t\r\n]+EXISTS)\w+", re.I
)


def _sql(text: str) -> str:
    return re.sub(r"--[^\n]*", "", text)


def non_idempotent(text: str) -> list[str]:
    """Операторы, которые второй прогон уронит."""
    sql = _sql(text)
    bad = [
        " ".join(match.group(0).split())
        for match in CREATE.finditer(sql)
        if "IF NOT EXISTS" not in match.group(1).upper()
    ]
    bad += [" ".join(match.group(0).split()) for match in ADD_COLUMN.finditer(sql)]
    return bad


def test_the_chain_is_not_empty() -> None:
    assert any(path.name == "011_stars_billing.sql" for path in CHAIN)


@pytest.mark.parametrize("path", CHAIN, ids=lambda path: path.name)
def test_a_migration_can_be_run_twice(path: Path) -> None:
    found = non_idempotent(path.read_text(encoding="utf-8"))

    assert not found, f"{path.name}: второй прогон упадёт на {found}"


def test_the_checker_itself_sees_what_would_break_a_second_run() -> None:
    """Проверяющий не должен быть всеядным: иначе тесты выше ничего не доказывают."""
    broken = """
        CREATE TABLE t (id INT);
        CREATE UNIQUE INDEX t_idx ON t (id);
        ALTER TABLE t ADD COLUMN extra TEXT;
    """
    fine = """
        CREATE TABLE IF NOT EXISTS t (id INT);
        CREATE UNIQUE INDEX IF NOT EXISTS t_idx ON t (id);
        ALTER TABLE t ADD COLUMN IF NOT EXISTS extra TEXT;
        -- CREATE TABLE в комментарии не считается
    """

    assert len(non_idempotent(broken)) == 3
    assert non_idempotent(fine) == []
