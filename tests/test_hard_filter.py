"""Жёсткий фильтр подписки: неизвестное отсеивается, место не расширяется, опечатка не молчит.

Тексты — как в живых объявлениях Нячанга на трёх языках; LLM и база не нужны.
"""

from __future__ import annotations

import pytest

from sniffer.domain.hard_filter import BALCONY, SEPARATE_KITCHEN, HardFilter
from sniffer.domain.records import Listing
from sniffer.worker.grant_watch import EXIT_USAGE, main, parse
from tests.monitor_support import listing

NORTH = frozenset({"vinh_hoa", "vinh_hai", "vinh_phuoc", "hon_xen", "pham_van_dong"})
OWNER = HardFilter(
    require=frozenset({BALCONY, SEPARATE_KITCHEN}),
    districts=NORTH,
    place_words=("duong de", "tran khat chan"),
)


def apartment(summary: str, *, district: str | None = "vinh_hoa", **attributes: object) -> Listing:
    return listing(
        1,
        category="apartment",
        deal_type="rent_out",
        title="Квартира",
        summary=summary,
        district=district,
        attributes=dict(attributes),
    )


def test_a_card_with_both_conditions_in_the_attributes_passes() -> None:
    card = apartment("", balcony=True, kitchen="separate")
    assert OWNER.accepts(card)


@pytest.mark.parametrize(
    "text",
    [
        "Квартира, отдельная кухня, большой балкон",
        "Apartment with a separate kitchen and balcony",
        "Căn hộ có ban công, bếp riêng, gần biển",
    ],
)
def test_the_conditions_are_read_from_ru_en_vi_text(text: str) -> None:
    assert OWNER.accepts(apartment(text))


@pytest.mark.parametrize(
    "text",
    [
        "Студия, балкон есть",  # кухня не названа отдельной: «неизвестно» = нет
        "Отдельная кухня, вид на море",  # балкон не назван
        "Без балкона, отдельная кухня",
        "No balcony, separate kitchen",
        "Балкон, кухня-студия",
        "Балкон, без отдельной кухни",
        "Балкон, кухня общая на этаже",
    ],
)
def test_unknown_or_negated_conditions_reject_the_card(text: str) -> None:
    assert not OWNER.accepts(apartment(text))


def test_an_attribute_beats_the_text() -> None:
    shared = apartment("Отдельная кухня, балкон", kitchen="shared")
    no_balcony = apartment("Отдельная кухня, балкон", kitchen="separate", balcony=False)
    assert not OWNER.accepts(shared)
    assert not OWNER.accepts(no_balcony)


def test_a_known_district_outside_the_list_is_rejected_even_with_the_conditions() -> None:
    south = apartment("Отдельная кухня, балкон", district="phuoc_long")
    assert not OWNER.accepts(south)


def test_a_named_street_by_the_landmark_overrides_the_guessed_district() -> None:
    text = "Отдельная кухня, балкон, Đường Đệ 12"
    assert OWNER.accepts(apartment(text, district=None))
    assert OWNER.accepts(apartment("Tran Khat Chan. Bếp riêng, ban công", district="oceanus"))


def test_an_unknown_district_without_a_landmark_is_rejected() -> None:
    assert not OWNER.accepts(apartment("Отдельная кухня, балкон", district=None))


def test_an_empty_filter_accepts_everything() -> None:
    assert HardFilter().accepts(apartment("ничего", district=None))


def test_json_round_trip_keeps_the_filter() -> None:
    assert HardFilter.from_json(OWNER.to_json()) == OWNER
    assert HardFilter.from_json(None) is None


@pytest.mark.parametrize(
    "raw",
    [
        {"require": ["balcon"]},  # опечатка не должна стать «фильтра нет»
        {"districts": ["nowhere"]},
        {"radius_m": 500},
        {"place_words": [" "]},
    ],
)
def test_a_broken_filter_raises_instead_of_passing_everything(raw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        HardFilter.from_json(raw)


def test_the_grant_command_reports_a_typo_as_usage_not_as_a_traceback() -> None:
    args = ["--tg-user-id", "1", "--root", "2", "--require", "balcon"]
    assert parse(args).require == ["balcon"]
    assert main(args) == EXIT_USAGE
