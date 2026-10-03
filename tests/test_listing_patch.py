"""Патч карточки: договор между выводом из текста и записью в базу.

Что проход догона вправе менять, записано здесь списком, а не угадывается из
кода: расширенный `PATCHABLE_COLUMNS` — решение, которое должно краснеть тестом,
а не проходить незамеченным.
"""

from __future__ import annotations

from dataclasses import fields
from decimal import Decimal

import pytest

from sniffer.domain.listing_patch import (
    PATCHABLE_COLUMNS,
    ListingPatch,
    PatchClash,
    PatchError,
)
from sniffer.domain.records import Listing
from tests.enrich_support import card


def test_the_pass_owns_exactly_these_columns() -> None:
    assert PATCHABLE_COLUMNS == {
        "price_amount",
        "price_currency",
        "price_period",
        "district",
        "title",
        "lang",
    }


# Состояние жизни и личность карточки: их решают воронка и проверка моделью
# (`is_active`, `screened_at`, `posted_at`, `deal_type`, `category`), а пересчёт
# по тексту их не трогает ни при каких условиях — список из задания, дословно.
@pytest.mark.parametrize(
    "column", ["is_active", "screened_at", "posted_at", "deal_type", "category", "city"]
)
def test_the_life_and_the_identity_of_a_card_are_not_the_passs_to_change(column: str) -> None:
    with pytest.raises(PatchError, match=column):
        ListingPatch(columns={column: "что-то"})


@pytest.mark.parametrize(
    "column", sorted(f.name for f in fields(Listing) if f.name not in PATCHABLE_COLUMNS)
)
def test_every_other_field_of_the_record_is_refused_too(column: str) -> None:
    """Новая колонка карточки по умолчанию неприкосновенна, пока её не внесли списком."""
    with pytest.raises(PatchError):
        ListingPatch(columns={column: None})


def test_attributes_travel_separately_from_columns() -> None:
    """`attributes` не колонка патча: у неё свой способ записи — слияние."""
    with pytest.raises(PatchError):
        ListingPatch(columns={"attributes": {"a": 1}})


def test_a_patch_with_no_changes_is_empty_even_if_it_reports_outcomes() -> None:
    patch = ListingPatch(outcomes=("price.same",))

    assert patch.is_empty
    assert patch.outcomes == ("price.same",)


def test_a_patch_with_a_column_an_attribute_or_a_removal_is_not_empty() -> None:
    assert not ListingPatch(columns={"lang": "ru"}).is_empty
    assert not ListingPatch(attributes={"area_m2": 30}).is_empty
    assert not ListingPatch(remove=("rate_per",)).is_empty, "одно удаление — тоже запись"


def test_merging_unites_columns_attributes_and_outcomes_in_order() -> None:
    first = ListingPatch({"lang": "ru"}, {"a": 1}, ("price.filled",))
    second = ListingPatch({"district": "north"}, {"b": 2}, ("district.filled",))

    merged = first.merged(second)

    assert dict(merged.columns) == {"lang": "ru", "district": "north"}
    assert dict(merged.attributes) == {"a": 1, "b": 2}
    assert merged.outcomes == ("price.filled", "district.filled")


def test_two_derivations_cannot_claim_one_column() -> None:
    with pytest.raises(PatchClash, match="title"):
        ListingPatch({"title": "A"}).merged(ListingPatch({"title": "B"}))


def test_two_derivations_cannot_claim_one_attribute_key_even_with_the_same_value() -> None:
    """Совпавшее значение — случайность, а не договорённость: молча слить нельзя."""
    with pytest.raises(PatchClash, match="floor"):
        ListingPatch(attributes={"floor": 2}).merged(ListingPatch(attributes={"floor": 2}))


def test_applying_overlays_columns_and_merges_attributes_keeping_the_rest() -> None:
    before = card(attributes={"rooms": 1, "furnished": True})
    patch = ListingPatch(
        {"price_amount": Decimal(9_000_000), "price_currency": "VND"},
        {"price_up_to": 11_000_000, "rooms": 2},
    )

    after = patch.applied_to(before)

    assert after.price_amount == Decimal(9_000_000)
    assert after.price_currency == "VND"
    # Как `attributes || :patch` в базе: правая сторона главнее, прочее цело.
    assert after.attributes == {"rooms": 2, "furnished": True, "price_up_to": 11_000_000}


def test_a_key_cannot_be_written_and_removed_in_the_same_patch() -> None:
    with pytest.raises(PatchError, match="rate_per"):
        ListingPatch(attributes={"rate_per": "day"}, remove=("rate_per",))


def test_removals_are_united_by_merging_and_claimed_like_any_other_key() -> None:
    first = ListingPatch(remove=("rate_amount",), outcomes=("price.same",))
    second = ListingPatch(remove=("floor",), outcomes=("facts.none",))

    assert first.merged(second).remove == ("rate_amount", "floor")
    with pytest.raises(PatchClash, match="rate_amount"):
        first.merged(ListingPatch(attributes={"rate_amount": 1}))
    with pytest.raises(PatchClash, match="rate_amount"):
        first.merged(ListingPatch(remove=("rate_amount",)))
    with pytest.raises(PatchClash, match="floor"):
        ListingPatch(attributes={"floor": 2}).merged(ListingPatch(remove=("floor",)))


def test_applying_removes_the_named_keys_before_it_merges_and_keeps_the_rest() -> None:
    before = card(
        attributes={"rate_amount": 250_000, "rate_per": "day", "brand": "honda", "rooms": 1}
    )
    patch = ListingPatch(
        attributes={"price_up_to": 11_000_000}, remove=("rate_amount", "rate_per", "absent_key")
    )

    after = patch.applied_to(before)

    # Как `(attributes - удаляемые) || патч` в базе; чужие ключи целы, а удаление
    # ключа, которого нет, — не ошибка.
    assert after.attributes == {"brand": "honda", "rooms": 1, "price_up_to": 11_000_000}


def test_applying_never_touches_another_field_and_never_mutates_the_original() -> None:
    before = card()
    original_attributes = dict(before.attributes)
    patch = ListingPatch({"lang": "ru"}, {"x": 1})

    after = patch.applied_to(before)

    changed = {f.name for f in fields(Listing) if getattr(before, f.name) != getattr(after, f.name)}
    assert changed == {"lang", "attributes"}
    assert before.attributes == original_attributes, "исходная карточка неизменна"
    assert before.lang is None
