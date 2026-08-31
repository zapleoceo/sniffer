"""Общее для всех таблиц: база деклараций, первичный ключ, серверные умолчания.

Умолчания вынесены сюда не ради экономии строк, а чтобы `'{}'::jsonb` в
двенадцати таблицах не разъехался в одиннадцать `'{}'::jsonb` и один `'{}'`.
"""

from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy import text as sa_text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Размерность эмбеддинга из DDL. Меняется только вместе с миграцией: старые
# векторы другой длины Postgres не примет.
EMBEDDING_DIM = 1024

NOW = sa_text("now()")
EMPTY_ARRAY = sa_text("'{}'")
EMPTY_JSON = sa_text("'{}'::jsonb")
TRUE = sa_text("TRUE")
FALSE = sa_text("FALSE")
ZERO = sa_text("0")


class Base(DeclarativeBase):
    pass


class BigIdMixin:
    """BIGSERIAL PRIMARY KEY — он одинаков у всех двенадцати таблиц."""

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
