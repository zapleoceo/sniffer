"""Карточка выдачи: факты, подпись источника, экранирование, длина сообщения."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import unescape

from sniffer.bot.cards import chunk, render_card, render_cards, source_label, visible_len
from sniffer.sources.base import RawItem

NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)
TELEGRAM_LIMIT = 4096


def item(**overrides: object) -> RawItem:
    values: dict[str, object] = {
        "source": "archive",
        "external_id": "1",
        "url": "https://t.me/c/1/2",
        "title": "Лот",
        "price_raw": "7 млн",
        "posted_at": NOW,
    }
    values.update(overrides)
    return RawItem(**values)  # type: ignore[arg-type]


def test_the_card_carries_facts_from_attributes() -> None:
    card = render_card(
        item(raw={"attributes": {"brand": "honda", "model": "lead", "engine_cc": 110}}), now=NOW
    )
    assert "Honda Lead · 110 cc" in card


def test_a_card_without_attributes_has_no_empty_line() -> None:
    assert "\n\n" not in render_card(item(), now=NOW)


def test_the_source_is_a_chat_name_or_a_board_not_a_service_word() -> None:
    assert source_label(item(raw={"chat_title": "  Viet   Avito "})) == "Viet Avito"
    assert source_label(item(source="chotot")) == "Chotot"
    assert source_label(item(source="archive")) == "Telegram"
    assert source_label(item(source="telegram_groups")) == "Telegram"


def test_text_from_a_stranger_cannot_break_the_markup() -> None:
    card = render_card(
        item(
            title="<b>A&B</b>",
            price_raw="<i>1</i>",
            raw={"chat_title": "<a href=x>", "attributes": {"brand": "<u>"}},
        ),
        now=NOW,
    )
    assert card.count("<b>") == 1
    assert "<i>" not in card and "<u>" not in card and "<a href=x>" not in card


def test_a_hostile_page_still_fits_the_telegram_limit() -> None:
    nasty = [
        item(
            external_id=str(number),
            title="&" * 300,
            price_raw="<" * 300,
            url="https://t.me/c/1/" + "9" * 80,
            raw={"chat_title": "&" * 300, "attributes": {"brand": "&" * 300, "model": "<" * 300}},
        )
        for number in range(5)
    ]
    assert visible_len(render_cards(nasty, now=NOW)) < TELEGRAM_LIMIT


def test_visible_length_counts_what_telegram_counts() -> None:
    assert visible_len('<b>a&amp;b</b> <a href="https://x.y/z">ссылка</a>') == len("a&b ссылка")


def test_chunks_split_on_card_borders_and_never_exceed_the_limit() -> None:
    blocks = [f"<b>{number}</b> " + "я" * 900 for number in range(12)]
    messages = chunk(blocks, head="шапка")
    assert len(messages) > 1
    assert all(visible_len(message) <= 4000 for message in messages)
    assert messages[0].startswith("шапка")
    assert sum("шапка" in message for message in messages) == 1
    rejoined = "\n\n".join(messages)
    assert all(re.search(rf"<b>{number}</b>", rejoined) for number in range(12))
    assert unescape(rejoined).count("я") == 12 * 900


def test_nothing_to_chunk_is_no_messages() -> None:
    assert chunk([]) == []
