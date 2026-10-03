"""Цена в проходе догона: политика замены накопленного значения.

Политика записана в `pipeline/enrich_price.py` и в `docs/architecture.md`
(5.0.4), и у каждой её строки здесь свой тест. Тесты проверяют ПОЛИТИКУ, а не
разбор: разбор текста подменён заглушкой, которая отдаёт заданный факт, —
поэтому правки самого разбора (ветка цены уточняется) их не ломают. Лишь пара
тестов в конце берёт настоящий разбор, чтобы показать, что провод цел.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sniffer.domain.listing_patch import ListingPatch
from sniffer.domain.price_bounds import price_bounds
from sniffer.domain.prices import PriceFact
from sniffer.pipeline.enrich_price import (
    ABSENT,
    DISAGREED,
    ERASED,
    FILLED,
    LOST,
    PRICE_ATTRIBUTES,
    RATE_ONLY,
    REPLACED,
    SAME,
    PriceDerivation,
    derive_price,
    judge,
    read_amount,
)
from sniffer.pipeline.listing_price import PriceColumns, price_columns
from tests.enrich_support import Parser, card, fact

# Квартира в аренду: границы здравого смысла, донги в месяц.
RENT_BOUNDS = price_bounds("apartment", "rent_out")
assert RENT_BOUNDS == (2_000_000, 150_000_000), "тесты ниже написаны под эти границы"
SELL_BOUNDS = price_bounds("apartment", "sell")
assert SELL_BOUNDS == (100_000_000, 10_000_000_000)

SOURCES = ["label", "weak", "money", "text", "footer"]
DAILY = PriceColumns(attributes={"rate_amount": 250_000, "rate_currency": "VND", "rate_per": "day"})


def monthly(amount: int) -> PriceColumns:
    return PriceColumns(Decimal(amount), "VND", "month")


def derived(listing_fields: dict[str, object], found: PriceFact | None) -> ListingPatch:
    return derive_price(card(**listing_fields), "текст", parse=Parser(found))  # type: ignore[arg-type]


# ── политика, строка за строкой ────────────────────────────────────────────


def test_an_empty_price_is_filled_by_a_found_amount() -> None:
    assert judge(None, monthly(9_000_000), bounds=RENT_BOUNDS) == FILLED


def test_an_empty_price_gets_only_a_rate_when_the_text_has_only_a_daily_one() -> None:
    assert judge(None, DAILY, bounds=RENT_BOUNDS) == RATE_ONLY


def test_an_empty_price_stays_empty_when_the_text_has_none() -> None:
    assert judge(None, PriceColumns(), bounds=RENT_BOUNDS) == ABSENT


def test_the_same_price_is_the_same() -> None:
    assert judge(Decimal("9000000.00"), monthly(9_000_000), bounds=RENT_BOUNDS) == SAME


def test_an_implausible_old_price_is_replaced_by_the_new_plausible_one() -> None:
    """«5500» вместо «5,5 млн»: ошибка прежнего разбора, ради которой проход и нужен."""
    assert judge(Decimal(5_500), monthly(5_500_000), bounds=RENT_BOUNDS) == REPLACED


def test_an_implausible_old_price_is_erased_when_the_text_gives_nothing() -> None:
    """Цена за пределами правдоподобия — заведомо ошибка: стирать её можно.

    5 млрд за аренду дома и 15 млн «разовой» цены у дома на продажу — не
    потеря, а мусор, и единственная честная замена ему — пустое место.
    """
    assert judge(Decimal(5_000_000_000), PriceColumns(), bounds=RENT_BOUNDS) == ERASED


def test_an_implausible_old_price_is_erased_when_the_text_gives_only_a_rate() -> None:
    """Суточная цена в месячной колонке — ровно такая ошибка; ставка уходит в атрибуты."""
    assert judge(Decimal(280_000), DAILY, bounds=RENT_BOUNDS) == ERASED


def test_a_new_price_that_is_implausible_too_replaces_nothing() -> None:
    """Замена обязана улучшать. Разбор такого не отдаёт, но политика не полагается на это."""
    verdict = judge(Decimal(5_500), monthly(900_000_000), bounds=RENT_BOUNDS)

    assert verdict == DISAGREED


def test_two_plausible_prices_that_differ_are_counted_never_changed() -> None:
    """Выбор наименьшей цены каталога вместо первой мог быть осознанным — не трогаем."""
    assert judge(Decimal(7_000_000), monthly(9_000_000), bounds=RENT_BOUNDS) == DISAGREED


def test_a_plausible_price_that_the_text_no_longer_shows_is_kept_and_counted() -> None:
    assert judge(Decimal(9_000_000), PriceColumns(), bounds=RENT_BOUNDS) == LOST
    assert judge(Decimal(9_000_000), DAILY, bounds=RENT_BOUNDS) == LOST


def test_without_bounds_nothing_is_implausible_so_nothing_is_replaced_or_erased() -> None:
    """Для пары без границ «вне границ» не определено: старое значение остаётся."""
    assert price_bounds("room", "sell") is None

    assert judge(Decimal(5_500), monthly(5_500_000), bounds=None) == DISAGREED
    assert judge(Decimal(5_500), PriceColumns(), bounds=None) == LOST


@pytest.mark.parametrize(
    ("old", "implausible"),
    [
        (RENT_BOUNDS[0], False),
        (RENT_BOUNDS[1], False),
        (RENT_BOUNDS[0] - 1, True),
        (RENT_BOUNDS[1] + 1, True),
    ],
)
def test_the_bounds_themselves_are_plausible(old: int, implausible: bool) -> None:
    """Границы включительно: ровно 2 млн — ещё аренда, 1 999 999 — уже нет."""
    replaced = judge(Decimal(old), monthly(9_000_000), bounds=RENT_BOUNDS)
    erased = judge(Decimal(old), PriceColumns(), bounds=RENT_BOUNDS)

    assert replaced == (REPLACED if implausible else DISAGREED)
    assert erased == (ERASED if implausible else LOST)


# ── вывод целиком: заданный факт → патч ────────────────────────────────────


@pytest.mark.parametrize("source", ["label", "weak", "money", "text", "footer"])
def test_a_replacement_does_not_depend_on_what_confirms_the_new_price(source: str) -> None:
    """Старое вне границ — заведомая ошибка: заменяет любая правдоподобная цена."""
    patch = derived({"price": 5_500}, fact(5_500_000, source=source))

    assert patch.outcomes == (REPLACED,)
    assert patch.columns == {"price_amount": Decimal(5_500_000)}


def test_a_fill_writes_dong_and_the_period_of_the_deal() -> None:
    patch = derived({}, fact(12_500_000, period="month"))

    assert dict(patch.columns) == {
        "price_amount": Decimal(12_500_000),
        "price_currency": "VND",
        "price_period": "month",
    }
    assert patch.outcomes == (FILLED,)


def test_a_replacement_writes_only_what_actually_differs() -> None:
    """Валюта и срок у «5500» уже верные — перезаписывается одна сумма."""
    patch = derived({"price": 5_500}, fact(5_500_000, period="month"))

    assert dict(patch.columns) == {"price_amount": Decimal(5_500_000)}


def test_an_erasure_empties_all_three_price_columns() -> None:
    patch = derived({"price": 5_000_000_000}, None)

    assert patch.outcomes == (ERASED,)
    assert dict(patch.columns) == {
        "price_amount": None,
        "price_currency": None,
        "price_period": None,
    }


def test_an_erasure_does_not_rewrite_columns_that_are_already_empty() -> None:
    listing = card(price=5_000_000_000, price_currency=None, price_period=None)

    patch = derive_price(listing, "текст", parse=Parser(None))

    assert dict(patch.columns) == {"price_amount": None}


@pytest.mark.parametrize("stored", [DISAGREED, LOST, SAME], ids=["disagreed", "lost", "same"])
def test_a_price_that_is_kept_or_already_right_changes_no_column(stored: str) -> None:
    found = {DISAGREED: fact(9_000_000), LOST: None, SAME: fact(7_000_000)}[stored]

    patch = derived({"price": 7_000_000}, found)

    assert patch.outcomes == (stored,)
    assert patch.columns == {}


def test_a_daily_price_goes_to_the_attributes_and_the_column_stays_empty() -> None:
    patch = derived({"category": "motorbike"}, fact(250_000, period="day"))

    assert patch.columns == {}
    assert dict(patch.attributes) == {
        "rate_amount": 250_000,
        "rate_currency": "VND",
        "rate_per": "day",
    }
    assert patch.outcomes == (RATE_ONLY,)


def test_dollars_go_to_the_attributes_too_and_never_pose_as_dong() -> None:
    patch = derived({}, fact(400, currency="USD", period="month"))

    assert patch.columns == {}
    assert patch.attributes["rate_currency"] == "USD"


def test_a_rate_next_to_a_stored_implausible_price_erases_it_and_keeps_the_rate() -> None:
    patch = derived({"category": "motorbike", "price": 200_000}, fact(250_000, period="day"))

    assert patch.outcomes == (ERASED,)
    assert patch.columns["price_amount"] is None
    assert patch.attributes["rate_amount"] == 250_000


# ── сторона и категория берутся у СТРОКИ, какие они сейчас ─────────────────


def test_the_current_side_and_category_of_the_card_are_what_the_parse_is_asked_about() -> None:
    """Вердикт модели мог сменить сторону; цена читается под итоговой, а не первой."""
    parser = Parser(fact(4_390_000_000))

    derive_price(card(deal_type="sell", category="apartment"), "текст", parse=parser)
    derive_price(card(deal_type="rent_out", category="house"), "текст", parse=parser)

    assert parser.asked == [("apartment", "sell"), ("house", "rent_out")]


def test_the_pass_never_recomputes_or_writes_the_side_and_the_category() -> None:
    patch = derived({"deal_type": "sell", "price": 13_000_000}, fact(4_390_000_000))

    assert set(patch.columns) <= {"price_amount", "price_currency", "price_period"}


def test_a_price_missed_under_the_old_side_is_filled_under_the_final_one() -> None:
    """Границы «аренда квартиры» выкинули 4,39 млрд; под итоговой стороной `sell` это цена."""
    patch = derived({"deal_type": "sell"}, fact(4_390_000_000))

    assert patch.outcomes == (FILLED,)
    assert patch.columns["price_amount"] == Decimal(4_390_000_000)
    assert patch.columns["price_period"] == "once", "срок следует за итоговой стороной"


def test_a_rent_stored_as_a_sale_price_is_replaced_by_the_real_sale_price() -> None:
    """13 000 000 `once` вместо 4 390 000 000: сумма аренды, застрявшая в цене продажи."""
    patch = derived({"deal_type": "sell", "price": 13_000_000}, fact(4_390_000_000))

    assert patch.outcomes == (REPLACED,)
    assert dict(patch.columns) == {"price_amount": Decimal(4_390_000_000)}


def test_an_amount_outside_the_final_bounds_is_erased_when_the_text_gives_nothing() -> None:
    """15 000 000 `once` у дома на продажу — не цена итоговой стороны, а разбор молчит."""
    patch = derived({"category": "house", "deal_type": "sell", "price": 15_000_000}, None)

    assert patch.outcomes == (ERASED,)
    assert patch.columns["price_amount"] is None


# ── атрибуты цены пересобираются из нового факта целиком ───────────────────


def test_the_upper_bound_of_a_range_comes_with_a_filled_price() -> None:
    patch = derived({}, fact(9_000_000, period="month", up_to=11_000_000))

    assert patch.columns["price_amount"] == Decimal(9_000_000)
    assert dict(patch.attributes) == {"price_up_to": 11_000_000}


def test_the_upper_bound_is_added_when_the_price_itself_already_matches() -> None:
    """Потерянное при смене категории возвращается из текста: цена та же, вилки нет."""
    patch = derived({"price": 9_000_000}, fact(9_000_000, period="month", up_to=11_000_000))

    assert patch.columns == {}
    assert dict(patch.attributes) == {"price_up_to": 11_000_000}
    assert patch.outcomes == (SAME,)


def test_a_rate_lost_with_the_attributes_is_restored_from_the_text() -> None:
    patch = derived(
        {"category": "motorbike", "attributes": {"brand": "honda"}}, fact(250_000, period="day")
    )

    assert dict(patch.attributes) == {
        "rate_amount": 250_000,
        "rate_currency": "VND",
        "rate_per": "day",
    }
    assert patch.remove == (), "чужие атрибуты не трогаем"


def test_attributes_that_are_already_right_are_not_written_again() -> None:
    patch = derived(
        {"price": 9_000_000, "attributes": {"price_up_to": 11_000_000}},
        fact(9_000_000, period="month", up_to=11_000_000),
    )

    assert patch.is_empty


def test_a_rate_of_the_former_side_does_not_outlive_a_found_price() -> None:
    """Сторона сменилась на продажу, цена нашлась, а суточная ставка прежней стороны осталась бы."""
    stale = {"rate_amount": 250_000, "rate_currency": "VND", "rate_per": "day", "brand": "honda"}

    patch = derived({"deal_type": "sell", "attributes": stale}, fact(21_000_000))

    assert patch.outcomes == (FILLED,)
    assert patch.attributes == {}
    assert set(patch.remove) == {"rate_amount", "rate_currency", "rate_per"}, "и только они"


def test_a_stale_upper_bound_goes_when_the_price_is_gone() -> None:
    patch = derived(
        {"price": 5_000_000_000, "attributes": {"price_up_to": 6_000_000_000, "rooms": 3}}, None
    )

    assert patch.outcomes == (ERASED,)
    assert patch.remove == ("price_up_to",)


def test_stale_attributes_of_a_card_without_any_price_are_cleaned_too() -> None:
    patch = derived({"attributes": {"rate_per": "day", "rooms": 2}}, None)

    assert patch.outcomes == (ABSENT,)
    assert patch.remove == ("rate_per",)
    assert patch.columns == {}


def test_a_kept_price_keeps_the_upper_bound_that_goes_with_it() -> None:
    """Верх вилки новой цены рядом с оставленной старой дал бы «7 млн — до 11 млн».

    Это две разные цены, склеенные в одну, а не диапазон из объявления. И
    уже стоящий `price_up_to` — от старой цены, которую мы оставили, — не стираем.
    """
    patch = derived(
        {"price": 7_000_000, "attributes": {"price_up_to": 8_000_000}},
        fact(9_000_000, period="month", up_to=11_000_000),
    )

    assert patch.outcomes == (DISAGREED,)
    assert patch.is_empty, "ни записи, ни удаления: вилка принадлежит старой цене"


def test_a_lost_price_keeps_the_upper_bound_that_goes_with_it() -> None:
    """Текст молчит, а правдоподобная цена осталась: её верх вилки тоже остаётся."""
    patch = derived({"price": 7_000_000, "attributes": {"price_up_to": 8_000_000}}, None)

    assert patch.outcomes == (LOST,)
    assert patch.is_empty, "ни записи, ни удаления"


def test_a_kept_price_still_has_its_rate_rebuilt() -> None:
    """Ставка от цены не зависит: чужая уходит, а найденная пишется."""
    patch = derived(
        {"price": 9_000_000, "attributes": {"rate_per": "week", "rate_amount": 1}},
        fact(250_000, period="day"),
    )

    assert patch.outcomes == (LOST,)
    assert patch.columns == {}
    assert dict(patch.attributes) == {
        "rate_amount": 250_000,
        "rate_currency": "VND",
        "rate_per": "day",
    }
    assert patch.remove == ()


def test_only_the_price_family_of_attributes_is_ever_removed() -> None:
    attributes = {"rate_per": "day", "brand": "honda", "rooms": 2, "furnished": True, "model": "x"}

    patch = derived({"price": 9_000_000, "attributes": attributes}, None)

    assert set(patch.remove) <= PRICE_ATTRIBUTES
    assert "brand" not in patch.remove and "rooms" not in patch.remove


def test_the_price_family_is_everything_the_price_columns_can_write_to_attributes() -> None:
    """Список ключей — знание о `price_columns`, а не отдельная выдумка.

    Ключ, который `price_columns` научится писать, но которого нет в семье, не
    пересобирался бы и не убирался бы — тест краснеет раньше, чем он осядет в базе.
    """
    facts = [
        fact(9_000_000, period="month", up_to=11_000_000),
        fact(250_000, period="day", up_to=300_000),
        fact(700_000, period="week"),
        fact(400, currency="USD", period="month", up_to=500),
        fact(2_000, currency="USD"),
        fact(9_000_000, up_to=11_000_000),
    ]
    written: set[str] = set()
    for deal_type in ("rent_out", "sell"):
        for one in facts:
            written |= set(price_columns(one, deal_type).attributes)

    assert written == PRICE_ATTRIBUTES


# ── идемпотентность ────────────────────────────────────────────────────────

IDEMPOTENT_CASES: list[tuple[dict[str, object], PriceFact | None]] = [
    ({}, fact(12_500_000, period="month")),
    ({"price": 5_500}, fact(5_500_000, period="month")),
    ({"price": 5_000_000_000}, None),
    ({}, fact(9_000_000, period="month", up_to=11_000_000)),
    ({"price": 9_000_000}, fact(9_000_000, period="month", up_to=11_000_000)),
    ({"price": 7_000_000}, fact(9_000_000, period="month", up_to=11_000_000)),
    ({"category": "motorbike"}, fact(250_000, period="day")),
    ({"category": "motorbike", "price": 200_000}, fact(250_000, period="day")),
    ({}, fact(400, currency="USD", period="month")),
    ({"price": 9_000_000}, None),
    ({}, None),
    ({"deal_type": "sell", "price": 13_000_000}, fact(4_390_000_000)),
    ({"deal_type": "sell"}, fact(4_390_000_000)),
    (
        {
            "deal_type": "sell",
            "attributes": {"rate_amount": 1, "rate_currency": "VND", "rate_per": "day", "x": 1},
        },
        fact(21_000_000),
    ),
    ({"attributes": {"rate_per": "day", "rooms": 2}}, None),
]


@pytest.mark.parametrize(("fields", "found"), IDEMPOTENT_CASES)
def test_a_second_pass_over_the_same_text_changes_nothing(
    fields: dict[str, object], found: PriceFact | None
) -> None:
    before = card(**fields)  # type: ignore[arg-type]
    first = derive_price(before, "текст", parse=Parser(found))

    after = first.applied_to(before)
    second = derive_price(after, "текст", parse=Parser(found))

    assert second.is_empty, "повторный проход ничего не пишет"


@pytest.mark.parametrize(("fields", "found"), IDEMPOTENT_CASES)
def test_the_patch_stays_inside_the_price_columns_and_the_price_family(
    fields: dict[str, object], found: PriceFact | None
) -> None:
    patch = derive_price(card(**fields), "текст", parse=Parser(found))  # type: ignore[arg-type]

    assert set(patch.columns) <= {"price_amount", "price_currency", "price_period"}
    assert (set(patch.attributes) | set(patch.remove)) <= PRICE_ATTRIBUTES


# ── провод: настоящий разбор, настоящий текст ──────────────────────────────


def test_the_registered_derivation_reads_the_text_with_the_real_parser() -> None:
    patch = PriceDerivation().derive(card(), "Oceanus.\nАрендная плата: 12.5 млн VND / месяц")

    assert patch.outcomes == (FILLED,)
    assert patch.columns["price_amount"] == Decimal(12_500_000)


def test_the_real_parser_is_asked_about_the_current_side_of_the_card() -> None:
    text = "Квартира в Нячанге.\nЦена: 4 390 000 000 VND"

    as_sale = PriceDerivation().derive(card(deal_type="sell"), text)
    as_rent = PriceDerivation().derive(card(deal_type="rent_out"), text)

    assert as_sale.outcomes == (FILLED,), "под продажей это цена"
    assert as_rent.outcomes == (ABSENT,), "под арендой 4,39 млрд — не цена"


# ── что дала бы колонка под другой парой ───────────────────────────────────


def test_the_amount_under_a_pair_is_what_the_price_column_would_hold() -> None:
    parser = Parser(fact(4_390_000_000))

    assert read_amount("текст", "apartment", "sell", parse=parser) == Decimal(4_390_000_000)
    assert parser.asked == [("apartment", "sell")], "спрашивали именно под этой парой"


def test_a_rate_or_a_missing_price_leaves_the_column_empty_under_any_pair() -> None:
    assert (
        read_amount("текст", "motorbike", "rent_out", parse=Parser(fact(250_000, period="day")))
        is None
    )
    assert read_amount("текст", "apartment", "rent_out", parse=Parser(None)) is None
