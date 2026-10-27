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
        "Без балкона, отдельная кухня",
        "No balcony, separate kitchen",
        "Không có ban công, bếp riêng",
        "Балкон, кухня-студия",
        "Балкон, без отдельной кухни",
        "Балкон, кухня общая на этаже",
    ],
)
def test_unknown_kitchen_or_negated_conditions_reject_the_card(text: str) -> None:
    assert not OWNER.accepts(apartment(text))


BALCONY_ONLY = HardFilter(require=frozenset({BALCONY}))  # политики нет: прежнее строгое
LENIENT = HardFilter(require=frozenset({BALCONY}), allow_unknown=frozenset({BALCONY}))
UNKNOWN_CARDS = [
    apartment("Тихая квартира", balcony=None),  # NULL
    apartment("Тихая квартира"),  # ключа нет
    apartment("Отдельная кухня, вид на море"),
]
DENIED_CARDS = [
    apartment("Тихая квартира", balcony=False),
    apartment("Без балкона"),
    apartment("No balcony"),
    apartment("Không có ban công"),
]


@pytest.mark.parametrize("card", UNKNOWN_CARDS)
def test_an_old_filter_still_rejects_an_unknown_balcony(card: Listing) -> None:
    assert not BALCONY_ONLY.accepts(card)
    assert BALCONY_ONLY.unconfirmed(card) == ()


def test_opt_in_balcony_true_passes_without_a_note() -> None:
    card = apartment("Тихая квартира", balcony=True)
    assert BALCONY_ONLY.accepts(card) and LENIENT.accepts(card)
    assert LENIENT.unconfirmed(card) == ()


@pytest.mark.parametrize("card", UNKNOWN_CARDS)
def test_opt_in_unknown_balcony_passes_with_the_unconfirmed_note(card: Listing) -> None:
    assert LENIENT.accepts(card)
    assert LENIENT.unconfirmed(card) == (BALCONY,)


@pytest.mark.parametrize("card", DENIED_CARDS)
def test_explicit_no_balcony_is_rejected_with_or_without_the_policy(card: Listing) -> None:
    assert not BALCONY_ONLY.accepts(card)
    assert not LENIENT.accepts(card)


def test_a_text_mention_confirms_the_balcony_without_a_note() -> None:
    card = apartment("Большой балкон")
    assert BALCONY_ONLY.accepts(card) and LENIENT.accepts(card)
    assert LENIENT.unconfirmed(card) == ()


def test_the_attribute_beats_a_negation_in_the_text_current_behaviour() -> None:
    """Фиксирует ТЕКУЩЕЕ поведение: `balcony=true` главнее слов «без балкона» в тексте."""
    card = apartment("Без балкона, тихо", balcony=True)
    assert BALCONY_ONLY.accepts(card) and LENIENT.accepts(card)
    assert LENIENT.unconfirmed(card) == ()


def test_the_policy_does_not_touch_kitchen_or_place() -> None:
    both = HardFilter(
        require=frozenset({BALCONY, SEPARATE_KITCHEN}),
        districts=frozenset({"vinh_hoa"}),
        allow_unknown=frozenset({BALCONY}),
    )
    assert not both.accepts(apartment("Тихая квартира", balcony=True))  # кухня неизвестна
    assert both.accepts(apartment("Тихая квартира", kitchen="separate"))
    assert both.unconfirmed(apartment("Тихая квартира", kitchen="separate")) == (BALCONY,)
    assert not both.accepts(apartment("Тихая квартира", kitchen="separate", district="phuoc_long"))
    assert not both.accepts(apartment("Тихая квартира", kitchen="separate", district=None))


def test_the_policy_survives_a_json_round_trip_and_is_absent_otherwise() -> None:
    assert HardFilter.from_json(LENIENT.to_json()) == LENIENT
    assert LENIENT.to_json()["allow_unknown"] == [BALCONY]
    assert "allow_unknown" not in BALCONY_ONLY.to_json()
    assert HardFilter.from_json({"require": ["balcony"]}) == BALCONY_ONLY


@pytest.mark.parametrize(
    "raw",
    [
        {"require": ["balcony"], "allow_unknown": ["separate_kitchen"]},
        {"require": ["balcony"], "allow_unknown": ["balcon"]},
        {"require": ["balcony"], "allow_unknown": "balcony"},
        {"require": ["balcony"], "allow_unknown": [1]},
        {"require": ["balcony"], "allow_unknown": False},
        {"require": ["balcony"], "allow_unknown": 0},
        {"require": ["balcony"], "allow_unknown": ""},
        {"require": ["balcony"], "allow_unknown": {}},
        {"require": ["balcony"], "allow_unknown": None},
        {"require": ["separate_kitchen"], "allow_unknown": ["balcony"]},
    ],
)
def test_an_invalid_policy_raises_so_the_subscription_is_quarantined(
    raw: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="hard_filter"):
        HardFilter.from_json(raw)


def test_the_delivered_card_says_the_balcony_is_unconfirmed_only_for_opt_in() -> None:
    from sniffer.notifier.delivery import UNCONFIRMED_BALCONY, render
    from sniffer.worker.monitor_queue import payload

    card = apartment("Тихая квартира")
    flagged = render(payload(card, unconfirmed=LENIENT.unconfirmed(card)))
    strict = render(payload(card, unconfirmed=BALCONY_ONLY.unconfirmed(card)))
    assert UNCONFIRMED_BALCONY == "⚠ балкон не подтверждён"
    assert UNCONFIRMED_BALCONY in flagged
    assert UNCONFIRMED_BALCONY not in strict
    assert "unconfirmed" not in payload(card)


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
