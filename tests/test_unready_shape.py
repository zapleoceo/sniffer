"""Форма запроса «первая карточка без вердикта»: без базы, по тексту SQL.

Условия подписки в нём недопустимы: ИИ-проверка и перекатегоризация меняют ровно эти
поля, и карточка, неподходящая сейчас, подходящей станет после смены категории
(ревью Opus волны 2, P5).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sniffer.db.repositories.listings import unready_statement

CUT = datetime(2026, 10, 4, tzinfo=UTC)


def sql() -> str:
    return str(unready_statement(after_id=5, verdict_before=CUT))


def test_the_query_does_not_look_at_what_the_subscription_asked() -> None:
    text = sql()
    for forbidden in ("category", "deal_type", "attributes", "city", "price_amount"):
        assert forbidden not in text, forbidden


def test_the_query_still_waits_only_for_unscreened_telegram_cards() -> None:
    text = sql()
    assert "screened_at IS NULL" in text
    assert "extracted_at >" in text
    assert "source =" in text
    assert "listings.id >" in text
