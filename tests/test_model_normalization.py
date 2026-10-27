"""PG1 / PG-1 / PG 1 — одна модель в обоих фильтрах (листинг 435947, подписка sub2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from sniffer.db import models
from sniffer.db.repositories.listings import _with_attributes
from sniffer.domain.model_key import model_aliases
from sniffer.domain.passport import Category, Intent, Passport
from sniffer.domain.records import Listing, MatchFilter
from sniffer.matching.rules import worth_sending

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def _listing(title: str, summary: str = "", **attributes: Any) -> Listing:
    return Listing(
        id=435947,
        raw_message_id=1,
        deal_type="sell",
        category="motorbike",
        city="nha_trang",
        title=title,
        summary=summary,
        tg_link="https://t.me/c/1/1",
        posted_at=NOW - timedelta(hours=1),
        attributes=attributes,
    )


def _passport(model: str) -> Passport:
    return Passport(
        intent=Intent.BUY,
        category=Category.MOTORBIKE,
        city="nha_trang",
        attributes={"model": model},
    )


def _sql(model: str) -> str:
    spec = MatchFilter(city="nha_trang", category="motorbike", deal_type="sell", model=model)
    statement = _with_attributes(select(models.Listing.id), spec)
    return str(
        statement.compile(
            dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
    )


def test_listing_435947_matches_passport_pg1_by_attribute() -> None:
    """Значения живой карточки: Yamaha PG-1, 2024, 10 000 км, 27 500 000 VND."""
    card = _listing(
        "Yamaha PG-1 2024, 10 000 km",
        "Продам Yamaha PG-1, 2024 год, пробег 10 000 км, 27 500 000 VND",
        brand="yamaha",
        model="pg-1",
        year=2024,
    )

    assert worth_sending(card, _passport("pg1"), now=NOW)


@pytest.mark.parametrize("wanted", ["PG1", "pg1", "PG-1", "PG 1", "pg_1"])
def test_every_spelling_of_the_wanted_model_matches_by_text(wanted: str) -> None:
    card = _listing("Yamaha PG-1 2019", "Продам Yamaha PG-1")

    assert worth_sending(card, _passport(wanted), now=NOW)


@pytest.mark.parametrize("text", ["Yamaha PG1", "Yamaha PG-1", "Yamaha PG 1", "yamaha pg-1 cub"])
def test_every_spelling_in_the_text_matches_passport_pg1(text: str) -> None:
    assert worth_sending(_listing(text), _passport("pg1"), now=NOW)


@pytest.mark.parametrize(
    "card",
    [
        _listing("Yamaha PG10", model="pg10"),
        _listing("Yamaha PG-10"),
        _listing("Yamaha PG2", model="pg2"),
        _listing("Yamaha PG-1X", model="pg-1x"),
        _listing("Yamaha PG 125"),
    ],
)
def test_other_models_do_not_match_pg1(card: Listing) -> None:
    assert not worth_sending(card, _passport("pg1"), now=NOW)


def test_other_models_keep_their_behaviour() -> None:
    assert worth_sending(_listing("Honda Lead 125 2020"), _passport("lead"), now=NOW)
    assert worth_sending(
        _listing("Honda Air Blade", model="air_blade"), _passport("air blade"), now=NOW
    )
    assert not worth_sending(_listing("Honda Vision"), _passport("lead"), now=NOW)


def test_non_pg1_python_gate_behaves_as_before() -> None:
    """Не-PG1: точная фраза, как на master. Слитное написание Python не принимает."""
    assert not worth_sending(_listing("Honda Airblade"), _passport("air_blade"), now=NOW)
    assert not worth_sending(_listing("Honda", model="airblade"), _passport("air_blade"), now=NOW)
    assert not worth_sending(_listing("Honda", model="mt15"), _passport("mt 15"), now=NOW)
    assert not worth_sending(_listing("Yamaha MT-15"), _passport("mt15"), now=NOW)
    assert worth_sending(_listing("Yamaha MT15"), _passport("mt15"), now=NOW)
    assert worth_sending(_listing("Honda Air Blade"), _passport("air_blade"), now=NOW)


def test_only_the_explicit_alias_list_is_widened() -> None:
    assert model_aliases("PG-1") == {"pg1", "pg-1", "pg 1", "pg_1"}
    assert model_aliases("mt15") == frozenset()
    assert model_aliases("air_blade") == frozenset()


def test_sql_prefilter_accepts_every_spelling_of_pg1() -> None:
    for wanted in ("pg1", "PG-1", "PG 1", "pg_1"):
        sql = _sql(wanted)
        assert "lower((listings.attributes ->> 'model')) IN" in sql
        for spelling in ("pg1", "pg-1", "pg 1", "pg_1"):
            assert f"'{spelling}'" in sql


def test_sql_prefilter_for_other_models_is_exactly_the_old_one() -> None:
    """Прежний SQL: точный атрибут + фраза + слитная фраза, без lower(...) IN."""
    sql = _sql("air_blade")
    assert "IN (" not in sql
    assert "'air blade'" in sql and "'airblade'" in sql
    assert "'pg-1'" not in sql
    assert "IN (" not in _sql("mt15")


def test_sql_prefilter_does_not_gain_other_models() -> None:
    assert "'pg10'" not in _sql("pg1")
    assert "'pg-1x'" not in _sql("pg1")
