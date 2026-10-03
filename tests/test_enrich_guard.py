"""Условие «строка не изменилась с момента чтения» — без живой базы.

Запись патча идёт рядом с живым воркером, и единственное, что отделяет её от
перетирания чужого решения, — `WHERE`, который повторяет прочитанное. Живая
проверка (`test_db_enrichment.py`) запускается только там, где есть Postgres, а
условие обязано краснеть на любой машине. Поэтому здесь три проверки, каждая из
которых ловит свой класс поломок:

* **состав** — какие колонки охраняются, сверен со списком из задачи, а не с
  кодом: убрать колонку из кода и оставить тест зелёным нельзя;
* **форма SQL** — компиляция настоящего `UPDATE` под Postgres: что именно он
  пишет и чем охраняется;
* **смысл** — то же условие, выполненное на настоящем движке SQL (SQLite с теми
  же колонками): каждая охраняемая колонка в отдельности превращает запись в
  отказ, а пустое значение само себе равно.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import asyncpg

from sniffer.db import models
from sniffer.db.repositories.listing_enrichment import (
    GUARD_COLUMNS,
    patch_statement,
    unchanged_since,
)
from sniffer.domain.listing_patch import PATCHABLE_COLUMNS, ListingPatch
from sniffer.domain.records import Listing
from tests.enrich_support import card

# Дословно из задания: что строка обязана сохранить, чтобы запись по ней была верна.
# Колонки, которые патч пишет, и колонки, которые вывод читает.
EXPECTED_GUARD = {
    "raw_message_id",
    "category",
    "deal_type",
    "city",
    "price_amount",
    "price_currency",
    "price_period",
    "district",
    "title",
    "lang",
    "attributes",
}
# Жизнь карточки не охраняется: гасить её можно в любой момент, и на верность патча
# это не влияет.
NOT_GUARDED = {"is_active", "screened_at", "posted_at", "extracted_at", "screen_note"}


def sql(statement: sa.Update) -> str:
    """Текст запроса так, как его соберёт боевой драйвер (asyncpg: `$1::NUMERIC`)."""
    return str(statement.compile(dialect=asyncpg.dialect()))  # type: ignore[no-untyped-call]


def set_clause(statement: sa.Update) -> str:
    match = re.search(r"SET (.*?) WHERE", sql(statement), re.DOTALL)
    assert match is not None
    return match.group(1)


# ── состав ─────────────────────────────────────────────────────────────────


def test_the_guard_is_exactly_what_the_pass_writes_and_what_it_reads() -> None:
    assert set(GUARD_COLUMNS) == EXPECTED_GUARD
    assert len(GUARD_COLUMNS) == len(set(GUARD_COLUMNS))


def test_the_guard_covers_every_column_the_pass_may_write() -> None:
    """Колонка, которую пишем, но не охраняем, — решение «заполнить» по устаревшему пустому."""
    assert set(GUARD_COLUMNS) >= PATCHABLE_COLUMNS | {"attributes"}


def test_the_guard_names_real_columns_of_the_table_and_real_fields_of_the_record() -> None:
    table_columns = set(models.Listing.__table__.c.keys())
    record_fields = set(Listing.__dataclass_fields__)

    assert set(GUARD_COLUMNS) <= table_columns
    assert set(GUARD_COLUMNS) <= record_fields


def test_the_life_of_a_card_is_not_guarded() -> None:
    assert not set(GUARD_COLUMNS) & NOT_GUARDED


# ── форма SQL ──────────────────────────────────────────────────────────────


def test_the_update_writes_only_what_the_patch_says() -> None:
    patch = ListingPatch({"price_amount": Decimal(9_000_000)}, {"price_up_to": 11_000_000})

    sets = set_clause(patch_statement(card(price=5_500), patch))

    assigned = {part.split("=")[0].strip() for part in re.split(r",\s*(?=\w+=)", sets)}
    assert assigned == {"price_amount", "attributes"}


@pytest.mark.parametrize(
    "column", ["is_active", "screened_at", "posted_at", "deal_type", "category", "city"]
)
def test_the_update_never_assigns_the_life_or_the_identity_of_a_card(column: str) -> None:
    patch = ListingPatch({"price_amount": Decimal(1)}, {"a": 1})

    assert column not in set_clause(patch_statement(card(), patch))


def test_attributes_are_merged_by_the_database_not_replaced() -> None:
    statement = patch_statement(card(), ListingPatch(attributes={"rate_amount": 250_000}))

    text = sql(statement)

    assert "attributes=(listings.attributes || $1::JSONB)" in text


def test_removed_keys_are_deleted_and_the_patch_merged_in_one_statement() -> None:
    patch = ListingPatch(attributes={"price_up_to": 11_000_000}, remove=("rate_amount", "rate_per"))

    statement = patch_statement(card(), patch)

    assert "attributes=((listings.attributes - $1::TEXT[]) || $2::JSONB)" in sql(statement)
    params = statement.compile(dialect=asyncpg.dialect()).params  # type: ignore[no-untyped-call]
    assert params["remove_keys"] == ["rate_amount", "rate_per"]


def test_a_patch_that_only_removes_keys_still_writes_the_attributes_column() -> None:
    statement = patch_statement(card(), ListingPatch(remove=("rate_amount",)))

    assert "attributes=(listings.attributes - $1::TEXT[])" in sql(statement)
    assert "||" not in sql(statement), "сливать нечего"


def test_a_patch_without_removals_does_not_subtract_anything() -> None:
    statement = patch_statement(card(), ListingPatch(attributes={"a": 1}))

    assert " - " not in set_clause(statement)


def test_a_patch_without_attributes_leaves_the_attributes_column_alone() -> None:
    statement = patch_statement(card(), ListingPatch({"lang": "ru"}))

    assert "attributes" not in set_clause(statement)


def test_every_guarded_column_is_compared_null_safely_in_the_where_clause() -> None:
    text = sql(patch_statement(card(listing_id=41), ListingPatch({"lang": "ru"})))
    where = text.split(" WHERE ", 1)[1]

    assert where.startswith("listings.id = ")
    for name in EXPECTED_GUARD:
        assert f"listings.{name} IS NOT DISTINCT FROM" in where, name


def test_an_empty_patch_is_refused_loudly_instead_of_writing_nothing() -> None:
    with pytest.raises(ValueError, match="пустой патч"):
        patch_statement(card(), ListingPatch(outcomes=("price.same",)))


# ── смысл: то же условие на настоящем движке SQL ───────────────────────────

# По одной причине на колонку: другое значение, как его мог оставить живой воркер.
CONCURRENT_CHANGE: dict[str, object] = {
    "raw_message_id": 9999,
    "category": "motorbike",
    "deal_type": "sell",
    "city": "da_nang",
    "price_amount": 123,
    "price_currency": "USD",
    "price_period": "once",
    "district": "north",
    "title": "Другой заголовок",
    "lang": "vi",
    "attributes": {"rooms": 9},
}
# Колонки, которые бывают пустыми, и пустое для них — реальное состояние карточки.
NULLABLE = ["raw_message_id", "price_amount", "price_currency", "price_period", "district", "lang"]


@dataclass
class Stand:
    """Таблица с теми же именами колонок, что у `listings`, на движке без Postgres."""

    engine: sa.Engine
    table: sa.Table

    def put(self, row: Listing) -> None:
        values = {name: getattr(row, name) for name in EXPECTED_GUARD}
        with self.engine.begin() as conn:
            conn.execute(sa.insert(self.table).values(id=row.id, **values))

    def change(self, **values: object) -> None:
        with self.engine.begin() as conn:
            conn.execute(sa.update(self.table).values(**values))

    def write(self, row: Listing) -> int:
        """Охраняемая запись; сколько строк она изменила."""
        statement = (
            sa.update(self.table).where(*unchanged_since(self.table, row)).values(title="ЗАПИСАНО")
        )
        with self.engine.begin() as conn:
            return conn.execute(statement).rowcount

    def titles(self) -> dict[int, str]:
        with self.engine.connect() as conn:
            return {
                int(listing_id): str(title)
                for listing_id, title in conn.execute(
                    sa.select(self.table.c.id, self.table.c.title)
                )
            }


@pytest.fixture
def stand() -> Stand:
    engine = sa.create_engine("sqlite://")
    meta = sa.MetaData()
    table = sa.Table(
        "listings",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("raw_message_id", sa.Integer),
        sa.Column("category", sa.Text),
        sa.Column("deal_type", sa.Text),
        sa.Column("city", sa.Text),
        sa.Column("price_amount", sa.Numeric(14, 2)),
        sa.Column("price_currency", sa.Text),
        sa.Column("price_period", sa.Text),
        sa.Column("district", sa.Text),
        sa.Column("title", sa.Text),
        sa.Column("lang", sa.Text),
        sa.Column("attributes", sa.JSON),
        sa.Column("is_active", sa.Boolean, server_default=sa.true()),
    )
    meta.create_all(engine)
    return Stand(engine, table)


@pytest.mark.filterwarnings("ignore:Dialect sqlite:sqlalchemy.exc.SAWarning")
class TestTheConditionOnARealEngine:
    def test_an_untouched_row_is_written(self, stand: Stand) -> None:
        row = card(price=5_500, attributes={"rooms": 2}, district="center", lang="ru")
        stand.put(row)

        assert stand.write(row) == 1
        assert stand.titles() == {41: "ЗАПИСАНО"}

    def test_a_row_with_empty_values_is_equal_to_itself(self, stand: Stand) -> None:
        """`NULL = NULL` — не истина: без null-безопасного сравнения такие строки не писались бы."""
        row = card(raw_message_id=None)  # цена, район, язык и сырьё пусты
        assert row.price_amount is None and row.district is None and row.lang is None
        stand.put(row)

        assert stand.write(row) == 1

    @pytest.mark.parametrize("column", sorted(CONCURRENT_CHANGE))
    def test_a_change_of_any_guarded_column_since_the_read_cancels_the_write(
        self, stand: Stand, column: str
    ) -> None:
        row = card(price=5_500, attributes={"rooms": 2}, district="center", lang="ru")
        stand.put(row)
        stand.change(**{column: CONCURRENT_CHANGE[column]})

        assert stand.write(row) == 0
        assert "ЗАПИСАНО" not in stand.titles().values(), "наша запись не легла"

    @pytest.mark.parametrize("column", [*NULLABLE, "attributes"])
    def test_a_value_that_appeared_where_there_was_none_cancels_the_write_too(
        self, stand: Stand, column: str
    ) -> None:
        """Прочитали пустое, пока считали — кто-то заполнил: решение «заполнить» устарело."""
        row = card(raw_message_id=None)
        stand.put(row)
        stand.change(**{column: CONCURRENT_CHANGE[column]})

        assert stand.write(row) == 0

    def test_the_life_of_the_card_does_not_cancel_the_write(self, stand: Stand) -> None:
        row = card()
        stand.put(row)
        stand.change(is_active=False)

        assert stand.write(row) == 1, "погасить карточку можно в любой момент"

    def test_another_row_is_never_written(self, stand: Stand) -> None:
        mine, other = card(41), card(42)
        stand.put(mine)
        stand.put(other)

        assert stand.write(mine) == 1
        assert stand.titles() == {41: "ЗАПИСАНО", 42: other.title}
