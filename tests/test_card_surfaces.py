"""Одни и те же факты на всех поверхностях: чат, мониторинг, отложенный ответ."""

from __future__ import annotations

from datetime import UTC, datetime

from sniffer.agent_app.followup import _item as deferred_item
from sniffer.domain.records import Listing
from sniffer.notifier.delivery import render
from sniffer.sources.base import RawItem
from sniffer.worker.monitor_queue import payload as monitor_payload

NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)
ATTRIBUTES = {"brand": "honda", "model": "lead", "engine_cc": 110, "year": 2008}


def test_a_monitor_card_carries_the_facts_of_the_listing() -> None:
    listing = Listing(
        raw_message_id=1,
        deal_type="sell",
        category="motorbike",
        city="nha_trang",
        title="Срочно продам",
        summary="",
        tg_link="https://t.me/c/1/2",
        posted_at=NOW,
        attributes=ATTRIBUTES,
    )
    text = render(monitor_payload(listing))
    assert "Honda Lead · 110 cc · 2008" in text


def test_a_deferred_answer_card_carries_the_same_facts() -> None:
    item = RawItem(
        source="archive",
        external_id="1",
        url="https://t.me/c/1/2",
        title="Срочно продам",
        raw={"attributes": ATTRIBUTES},
    )
    text = render(deferred_item(item))
    assert "Honda Lead · 110 cc · 2008" in text


def test_a_card_without_attributes_gets_no_facts_line_and_stays_valid() -> None:
    item = RawItem(source="chotot", external_id="1", url="https://x.y/1", title="Лот")
    assert deferred_item(item)["facts"] == ""
    assert "\n\n" not in render(deferred_item(item))


def test_facts_from_a_stranger_cannot_break_the_markup() -> None:
    text = render({"title": "Лот", "url": "https://x.y", "facts": "<b>x</b> & <i>y"})
    assert "<i>" not in text and "&lt;b&gt;x&lt;/b&gt; &amp; &lt;i&gt;y" in text
