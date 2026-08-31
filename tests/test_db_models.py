"""ORM-модели против `001_init.sql` — без базы.

Схему создаёт SQL-файл, модели используются для запросов. Разъехавшись, они
дают не ошибку импорта, а ошибку в рантайме на живом клиенте, поэтому имена
таблиц и колонок сверяются прямо с DDL.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import UniqueConstraint

from sniffer.db.models import Base

SCHEMA = Path(__file__).resolve().parents[1] / "infra" / "sql" / "001_init.sql"

# Строки внутри CREATE TABLE, которые описывают не колонку, а ограничение.
NOT_A_COLUMN = ("unique", "primary", "foreign", "check", "constraint", "exclude")


def _sql_tables() -> dict[str, set[str]]:
    """Таблицы и их колонки, как они записаны в DDL."""
    body = re.sub(r"--[^\n]*", "", SCHEMA.read_text(encoding="utf-8"))
    tables: dict[str, set[str]] = {}
    for match in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);", body, re.DOTALL):
        name, columns = match.group(1), set()
        for line in match.group(2).splitlines():
            head = line.strip().split(" ")[0].strip(",").lower()
            if head and head.isidentifier() and head not in NOT_A_COLUMN:
                columns.add(head)
        tables[name] = columns
    return tables


def test_all_tables_are_mirrored() -> None:
    assert set(Base.metadata.tables) == set(_sql_tables())


def test_columns_match_ddl() -> None:
    for name, columns in _sql_tables().items():
        assert {c.name for c in Base.metadata.tables[name].columns} == columns, name


def test_raw_messages_dedup_key_is_unique() -> None:
    """Батч-вставка сырья опирается на этот индекс: без него дедуп молчит."""
    keys = {
        tuple(sorted(c.name for c in constraint.columns))
        for constraint in Base.metadata.tables["raw_messages"].constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("chat_tg_id", "msg_id") in keys


def test_listings_one_card_per_raw_message() -> None:
    unique = {
        tuple(sorted(c.name for c in constraint.columns))
        for constraint in Base.metadata.tables["listings"].constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("raw_message_id",) in unique
