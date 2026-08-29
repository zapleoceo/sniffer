"""Карточка выдачи и честная пометка о возрасте.

Пометка проверяется отдельно и придирчиво: она единственное, что стоит между
клиентом и звонком по лоту, проданному два месяца назад (spec-v2, 3.3).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sniffer.bot.cards import MAX_CARDS, render_card, render_cards
from sniffer.sources.base import RawItem
from sniffer.verifier.liveness import STALE_AFTER_DAYS, Liveness, assess

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def item(**overrides: object) -> RawItem:
    fields: dict[str, object] = {
        "source": "chotot",
        "external_id": "123",
        "url": "https://www.chotot.com/123.htm",
        "title": "Honda Vision 2019 chính chủ",
        "text": "Xe đẹp, máy êm, giấy tờ đầy đủ, liên hệ 090xxxxxxx",
        "price_raw": "25.000.000 đ",
        "price_vnd": 25_000_000,
        "posted_at": NOW - timedelta(days=2),
    }
    fields.update(overrides)
    return RawItem(**fields)  # type: ignore[arg-type]


def test_card_has_title_price_date_and_link() -> None:
    card = render_card(item(), now=NOW)

    assert "Honda Vision 2019" in card
    assert "25.000.000 đ" in card
    assert "27.08.2026" in card
    assert 'href="https://www.chotot.com/123.htm"' in card


def test_card_does_not_reprint_the_ad() -> None:
    """Отдаём ссылку, а не чужой текст с контактами (architecture.md, раздел 1)."""
    card = render_card(item(), now=NOW)

    assert "090xxxxxxx" not in card
    assert "máy êm" not in card


def test_fresh_ad_has_no_warning() -> None:
    card = render_card(item(posted_at=NOW - timedelta(days=STALE_AFTER_DAYS)), now=NOW)

    assert "могло быть продано" not in card


def test_old_ad_says_how_old_it_is() -> None:
    card = render_card(item(posted_at=NOW - timedelta(days=20)), now=NOW)

    assert "объявлению 20 дней, могло быть продано" in card


@pytest.mark.parametrize(
    ("days", "expected"),
    [(15, "15 дней"), (21, "21 день"), (22, "22 дня"), (25, "25 дней"), (111, "111 дней")],
)
def test_age_is_declined(days: int, expected: str) -> None:
    """Бот, который пишет «21 дней», выглядит сломанным."""
    card = render_card(item(posted_at=NOW - timedelta(days=days)), now=NOW)

    assert f"объявлению {expected}, могло быть продано" in card


def test_ad_without_date_is_marked_unverified() -> None:
    """Живость unknown не выбрасывает карточку, но показывается с пометкой."""
    card = render_card(item(posted_at=None), now=NOW)

    assert "дата публикации неизвестна" in card
    assert "могло быть продано" not in card


def test_price_falls_back_to_number_then_to_honesty() -> None:
    assert "25 000 000 ₫" in render_card(item(price_raw=""), now=NOW)
    assert "цена не указана" in render_card(item(price_raw="", price_vnd=None), now=NOW)


def test_title_is_escaped_and_trimmed() -> None:
    card = render_card(item(title="<b>СРОЧНО</b> " + "очень длинный заголовок " * 10), now=NOW)

    assert "<b>&lt;b&gt;СРОЧНО" in card
    assert card.count("<b>") == 1
    assert "…" in card


def test_outputs_no_more_than_five_cards() -> None:
    items = [item(external_id=str(index)) for index in range(12)]

    cards = render_cards(items, now=NOW)

    assert cards.count("открыть оригинал") == MAX_CARDS


def test_naive_timestamp_does_not_crash_the_answer() -> None:
    """Источник может отдать метку без зоны — считаем её UTC, а не падаем."""
    verdict = assess(datetime(2026, 8, 1, 12, 0), now=NOW)

    assert verdict.status is Liveness.STALE
    assert verdict.age_days == 28


def test_timestamp_from_the_future_is_not_negative_age() -> None:
    verdict = assess(NOW + timedelta(days=3), now=NOW)

    assert verdict.status is Liveness.FRESH
    assert verdict.age_days == 0
