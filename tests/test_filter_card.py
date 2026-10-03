"""Карточка фильтра: чипы по заданным условиям, честность про монитор, провод кнопок."""

from __future__ import annotations

import pytest
from aiogram.types import InlineKeyboardMarkup

from sniffer.bot import filter_card as card
from sniffer.domain.field_spec import spec_by_key
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport


def view(
    passport: Passport | None = None, *, version: int = 3, monitoring: str = "off"
) -> card.CardView:
    base = passport or Passport(
        intent=Intent.BUY,
        category=Category.MOTORBIKE,
        city="nha_trang",
        budget=Budget(max=15_000_000, currency=Currency.VND),
        attributes={"brand": "honda", "transmission": "automatic"},
        districts=["Loc Tho"],
        raw_query="скутер",
    )
    return card.CardView(root=77, version=version, passport=base, monitoring=monitoring)


def buttons(markup: InlineKeyboardMarkup) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data or "") for row in markup.inline_keyboard for b in row]


def test_text_lists_only_filled_conditions_with_readable_values() -> None:
    text = card.card_text(view())
    assert "Коробка: автомат" in text
    assert "Бюджет до: 15 000 000 VND" in text
    assert "Марка: honda" in text
    assert "Бассейн" not in text


def test_a_condition_the_monitor_ignores_says_so() -> None:
    text = card.card_text(view())
    line = next(x for x in text.splitlines() if x.startswith("• Районы"))
    assert card.NOT_MONITORED in line
    assert card.NOT_MONITORED not in next(x for x in text.splitlines() if "Коробка" in x)


def test_text_is_html_safe_for_words_from_the_client() -> None:
    passport = Passport(category=Category.MOTORBIKE, city="x", districts=["<b>&"], attributes={})
    text = card.card_text(view(passport))
    assert "<b>&" not in text.split("\n", 2)[2]
    assert "&lt;b&gt;&amp;" in text


def test_every_filled_condition_gets_a_chip_and_a_remove_button_except_required() -> None:
    markup = card.card_markup(view(), footer=[])
    packed = [card.FilterCallback.unpack(data) for _, data in buttons(markup)]
    removable = {p.f for p in packed if p.a == card.CLEAR}
    opened = {p.f for p in packed if p.a == card.OPEN}
    assert opened == {"city", "budget_max", "districts", "brand", "transmission"}
    assert removable == opened - {"city"}


def test_buttons_carry_the_version_the_person_looked_at() -> None:
    markup = card.card_markup(view(version=9), footer=[])
    assert {card.FilterCallback.unpack(d).v for _, d in buttons(markup)} == {9}


def test_footer_rows_come_after_the_chips() -> None:
    from aiogram.types import InlineKeyboardButton

    foot = [[InlineKeyboardButton(text="footer", callback_data="x")]]
    assert buttons(card.card_markup(view(), footer=foot))[-1] == ("footer", "x")


def test_an_empty_filter_says_so_and_offers_to_add() -> None:
    empty = view(Passport(category=Category.BICYCLE))
    assert "Условий пока нет" in card.card_text(empty)
    assert any(
        card.FilterCallback.unpack(d).a == card.PICK
        for _, d in buttons(card.card_markup(empty, footer=[]))
    )


def test_a_choice_field_offers_its_options_and_any() -> None:
    spec = spec_by_key("transmission")
    assert spec is not None
    packed = [card.FilterCallback.unpack(d) for _, d in buttons(card.field_markup(view(), spec))]
    assert {p.o for p in packed if p.a == card.SET} == {"automatic", "manual", "semi"}
    assert any(p.a == card.CLEAR for p in packed)


def test_a_flag_offers_yes_and_no_and_parses_back_to_a_bool() -> None:
    spec = spec_by_key("pool")
    assert spec is not None
    packed = [card.FilterCallback.unpack(d) for _, d in buttons(card.field_markup(view(), spec))]
    assert {p.o for p in packed if p.a == card.SET} == {"true", "false"}
    assert card.parse_option(spec, "true") is True
    assert card.parse_option(spec, "false") is False


def test_the_city_has_no_any_button() -> None:
    spec = spec_by_key("city")
    assert spec is not None
    markup = card.field_markup(view(), spec)
    assert all(card.FilterCallback.unpack(d).a != card.CLEAR for _, d in buttons(markup))


def test_the_picker_lists_conditions_not_yet_set() -> None:
    labels = [text for text, _ in buttons(card.addable_markup(view()))]
    assert "Модель" in labels and "Коробка" not in labels


@pytest.mark.parametrize("a", [card.OPEN, card.SET, card.CLEAR, card.PICK, card.BACK])
def test_actions_are_distinct_single_letters(a: str) -> None:
    assert len(a) == 1
    assert len({card.OPEN, card.SET, card.CLEAR, card.PICK, card.BACK}) == 5
