"""ORM-модели против всех аддитивных миграций — без базы.

Схему создаёт SQL-файл, модели используются для запросов. Разъехавшись, они
дают не ошибку импорта, а ошибку в рантайме на живом клиенте, поэтому имена
таблиц и колонок сверяются прямо с DDL.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import UniqueConstraint

from sniffer.db import collection_models as _collection_models  # noqa: F401
from sniffer.db.models import Base

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "infra" / "sql"

# Строки внутри CREATE TABLE, которые описывают не колонку, а ограничение.
NOT_A_COLUMN = ("unique", "primary", "foreign", "check", "constraint", "exclude")


def _ddl() -> str:
    """Схема целиком, без комментариев: одним текстом её читают оба теста ниже."""
    return re.sub(
        r"--[^\n]*",
        "",
        "\n".join(path.read_text(encoding="utf-8") for path in sorted(SCHEMA_DIR.glob("00*.sql"))),
    )


def _sql_tables() -> dict[str, set[str]]:
    """Таблицы и их колонки, как они записаны в DDL."""
    body = _ddl()
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


def test_every_deploy_sentinel_names_a_real_column() -> None:
    """Колонки-часовые деплоя существуют в схеме, и доказывает это не память.

    Часовой — это проверка в `infra/deploy.sh`, которая валит деплой, если ALTER
    не доехал до живой базы (живой отказ 02.09.2026: таблицы были, колонок нет, а
    деплой рапортовал успех). У такой проверки есть своя беда: опечатка в имени
    колонки делает её вечно красной, а переименованная колонка — вечно зелёной,
    и в обоих случаях часовой перестаёт охранять, ничего об этом не сказав.
    Поэтому пары «таблица — колонка» читаются из самого скрипта и сверяются с
    моделями, а не выписываются сюда списком: список — это снова то, что вспомнили.
    """
    script = (SCHEMA_DIR.parent / "deploy.sh").read_text(encoding="utf-8")
    sentinels = re.findall(r"table_name='(\w+)' and column_name='(\w+)'", script)

    assert sentinels, "часовых не нашлось — проверка охраняет пустоту"
    for table, column in sentinels:
        assert table in Base.metadata.tables, table
        assert column in {c.name for c in Base.metadata.tables[table].columns}, f"{table}.{column}"
        assert f"ADD COLUMN IF NOT EXISTS {column}" in _ddl(), (
            f"{table}.{column}: часовой стоит на колонке без идемпотентного ALTER — "
            "на свежей базе он зелёный, на живой красный"
        )


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
