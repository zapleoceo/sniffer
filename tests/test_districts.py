"""Справочник мест: инварианты данных, которые ломают чтение молча.

Справочник — это данные, а данные портятся тихо: два района с одним слагом склеят карточки
в базе, а написание с диакритикой или заглавными буквами просто не найдётся, и никто не
заметит, пока район не пропадёт у тысячи карточек. Поэтому проверяется не «что нашлось в
одном посте», а форма самого справочника.
"""

from __future__ import annotations

import re
from collections import Counter

from sniffer.domain.districts import (
    CITY_DATA,
    KIND_RANK,
    PLACE_BY_SLUG,
    PLACES,
    ZONE_NAMES,
    ZONE_PHRASES,
    ZONE_WORDS,
)
from sniffer.domain.facts_place import _BY_ALIAS, _norm
from sniffer.domain.facts_text import fold

ZONES = {"north", "center", "south", "west"}


def test_every_slug_is_one_place_in_the_whole_registry() -> None:
    """Два города с одним слагом склеили бы в `listings.district` разные районы."""
    counts = Counter(place.slug for place in PLACES)

    assert [slug for slug, count in counts.items() if count > 1] == []
    assert len(PLACE_BY_SLUG) == len(PLACES)


def test_a_slug_is_a_plain_ascii_word_for_the_database_and_for_url_parts() -> None:
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", place.slug) for place in PLACES)


def test_every_place_has_a_name_a_known_kind_and_a_city_of_the_registry() -> None:
    for place in PLACES:
        assert place.name.strip(), place.slug
        assert place.kind in KIND_RANK, place.slug
        assert place.city in CITY_DATA, place.slug
        assert place.aliases, place.slug


def test_a_zone_is_one_of_the_four_and_only_nha_trang_has_zones() -> None:
    """У Дананга другая география: назвать его район «севером» значило бы выдумать."""
    for place in PLACES:
        assert place.zone is None or place.zone in ZONES, place.slug
        assert place.zone is None or place.city == "nha_trang", place.slug
    assert set(ZONE_NAMES) == ZONES
    assert set(ZONE_PHRASES) == ZONES
    assert set(ZONE_WORDS) == ZONES


def test_an_alias_is_already_folded_so_that_it_can_be_found() -> None:
    """Текст сворачивается (`fold`), и написание с диакритикой или заглавными не найдётся."""
    for place in PLACES:
        for alias in place.aliases:
            assert alias == fold(alias), (place.slug, alias)
            assert alias == _norm(alias), (place.slug, alias)


def test_no_alias_belongs_to_two_places() -> None:
    """Словарь «написание → место» молча перезаписывает дубликат: проверяем до перезаписи."""
    aliases = [alias for place in PLACES for alias in place.aliases]

    assert len(aliases) == len(_BY_ALIAS)


def test_a_complex_or_a_street_is_never_weaker_than_the_ward_it_stands_in() -> None:
    assert KIND_RANK["area"] < KIND_RANK["complex"] < KIND_RANK["street"]
