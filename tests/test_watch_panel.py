"""Панель «Мои слежения»: слоты, статусы, управление; предел поисков 5 / 10."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aiogram.types import InlineKeyboardMarkup

from sniffer.bot import watch_panel as panel
from sniffer.domain import plans
from sniffer.domain.passport import Category, Intent, Passport
from sniffer.domain.records import QueryOverview


def search(root: int, monitoring: str = "off", city: str = "nha_trang") -> QueryOverview:
    passport = Passport(intent=Intent.BUY, category=Category.MOTORBIKE, city=city, raw_query="x")
    expires = datetime.now(UTC) + timedelta(days=5) if monitoring in {"active", "paused"} else None
    return QueryOverview(root=root, passport=passport, monitoring=monitoring, expires_at=expires)


def data(markup: InlineKeyboardMarkup) -> list[panel.WatchCallback | str]:
    out: list[panel.WatchCallback | str] = []
    for row in markup.inline_keyboard:
        for b in row:
            cb = b.callback_data or ""
            out.append(panel.WatchCallback.unpack(cb) if cb.startswith("wch:") else cb)
    return out


def test_the_search_limit_is_five_free_and_ten_paid() -> None:
    assert plans.search_cap(0) == 5
    assert plans.search_cap(1) == 10
    assert plans.search_cap(4) == 10
    with pytest.raises(ValueError):
        plans.search_cap(-1)


def test_panel_text_shows_slots_limit_and_status_per_search() -> None:
    items = [search(1, "active"), search(2, "paused", "da_nang"), search(3)]
    text = panel.panel_text(panel.PanelView(items, paid_slots=2, bound_slots=2, used=3, cap=10))
    assert "куплено 2, занято 2" in text
    assert "Поисков: 3 из 10" in text
    assert "🟢" in text and "⏸" in text and "▫️" in text


def test_an_empty_panel_invites_to_search() -> None:
    text = panel.panel_text(panel.PanelView([], 0, 0, 0, 5))
    assert panel.EMPTY in text


def test_every_search_opens_its_card_and_new_search_is_last() -> None:
    view = panel.PanelView([search(1), search(2, "active")], 1, 1, 2, 10)
    actions = data(panel.panel_markup(view))
    assert [(a.a, a.root) for a in actions[:2]] == [(panel.CARD, 1), (panel.CARD, 2)]  # type: ignore[union-attr]
    assert actions[-1].a == panel.NEW  # type: ignore[union-attr]


def test_active_search_offers_pause_and_move_paused_offers_resume() -> None:
    active = data(InlineKeyboardMarkup(inline_keyboard=panel.card_footer(search(1, "active"))))
    paused = data(InlineKeyboardMarkup(inline_keyboard=panel.card_footer(search(1, "paused"))))
    assert {a.a for a in active if not isinstance(a, str)} >= {
        panel.PAUSE,
        panel.MOVE,
        panel.DELETE,
    }
    assert {a.a for a in paused if not isinstance(a, str)} >= {
        panel.RESUME,
        panel.MOVE,
        panel.DELETE,
    }


def test_a_search_without_a_slot_offers_to_subscribe_with_the_price() -> None:
    rows = panel.card_footer(search(1, "off"))
    first = rows[0][0]
    assert str(plans.SUBSCRIPTION_STARS) in first.text
    assert (first.callback_data or "").startswith("sub:")


def test_move_targets_are_searches_without_a_live_slot() -> None:
    source = search(1, "active")
    others = [source, search(2), search(3, "paused"), search(4, "expired")]
    targets = [
        a.to
        for a in data(panel.move_markup(source, others))
        if not isinstance(a, str) and a.a == panel.MOVE_TO
    ]
    assert targets == [2, 4]


def test_delete_asks_for_confirmation_and_warns_about_a_paid_slot() -> None:
    assert "слот" in panel.delete_text(search(1, "active"))
    assert "слот" not in panel.delete_text(search(1, "off"))
    markup = panel.delete_markup(5)
    confirm = next(a for a in data(markup) if not isinstance(a, str) and a.a == panel.DELETE_OK)
    assert confirm.root == 5


def test_the_limit_message_mentions_the_paid_limit_only_to_free_accounts() -> None:
    free = panel.LIMIT_REACHED.format(
        used=5, cap=5, upgrade=panel.UPGRADE.format(paid=panel.PAID_SEARCHES)
    )
    paid = panel.LIMIT_REACHED.format(used=10, cap=10, upgrade="")
    assert "до 10" in free and "до 10" not in paid


def test_names_with_markup_are_escaped_in_the_text_but_not_in_buttons() -> None:
    passport = Passport(raw_query="<b>x</b> & y")
    item = QueryOverview(root=1, passport=passport)
    text = panel.panel_text(panel.PanelView([item], 0, 0, 1, 5))
    assert "&lt;b&gt;x&lt;/b&gt;" in text
    button = panel.panel_markup(panel.PanelView([item], 0, 0, 1, 5)).inline_keyboard[0][0]
    assert "<b>" in button.text
