"""Миграция безопасности доставки: только аддитивный DDL, и тот же DDL в основной схеме.

Файл `014_notifier_safety.sql` лежит в цепочке, которую деплой гонит на КАЖДОМ
запуске, поэтому правка данных в нём исполнялась бы после каждого деплоя. Обе
колонки продублированы в `001_init.sql` намеренно: пока цепочка применялась по
прежней узкой маске, файл 014 деплой не брал, и без дубля нотифаер упал бы на
колонке, которой нет. Дубль без сторожа разъедется — это и есть сторож.
"""

from __future__ import annotations

import re
from pathlib import Path

from sniffer.db.models import Base

SQL_DIR = Path(__file__).resolve().parents[1] / "infra" / "sql"
MIGRATION = SQL_DIR / "014_notifier_safety.sql"
BASE = SQL_DIR / "001_init.sql"

_ADD_COLUMN = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+([^;]+);", re.I
)
FORBIDDEN = ("update", "delete", "insert", "truncate", "drop", "do $$")


def _code(path: Path) -> str:
    """Файл без комментариев и с одним пробелом между словами."""
    text = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
    return re.sub(r"\s+", " ", text).strip()


def _columns(path: Path) -> dict[tuple[str, str], str]:
    return {
        (table.lower(), column.lower()): definition.strip().lower()
        for table, column, definition in _ADD_COLUMN.findall(_code(path))
    }


def test_the_migration_adds_columns_and_nothing_else() -> None:
    statements = [part.strip() for part in _code(MIGRATION).split(";") if part.strip()]

    assert statements, "миграция пуста: проверка охраняет пустоту"
    assert all(_ADD_COLUMN.fullmatch(part + ";") for part in statements), statements
    for verb in FORBIDDEN:
        assert verb not in _code(MIGRATION).lower(), f"{verb!r}: цепочка идёт на каждом деплое"


def test_the_migration_names_exactly_the_columns_the_notifier_needs() -> None:
    assert set(_columns(MIGRATION)) == {("users", "bot_blocked_at"), ("outbox", "last_error")}


def test_the_base_schema_repeats_every_column_with_the_same_definition() -> None:
    """Разное определение в двух файлах — это два разных столбца в двух разных базах."""
    base = _columns(BASE)

    for key, definition in _columns(MIGRATION).items():
        assert base.get(key) == definition, f"{key}: в 001_init.sql {base.get(key)!r}"


def test_the_columns_are_nullable_so_existing_rows_stay_valid() -> None:
    users, outbox = Base.metadata.tables["users"], Base.metadata.tables["outbox"]

    assert users.c.bot_blocked_at.nullable and outbox.c.last_error.nullable
    assert users.c.bot_blocked_at.server_default is None, "«не заблокирован» — это NULL"
