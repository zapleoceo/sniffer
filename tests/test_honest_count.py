"""Честный счёт в заголовке: «не меньше N», когда источник отдал потолок своей выборки."""

from __future__ import annotations

from sniffer.bot import wording
from sniffer.domain.passport import Category, Passport


def test_a_capped_count_says_at_least_and_an_exact_one_does_not() -> None:
    passport = Passport(category=Category.MOTORBIKE, attributes={"brand": "honda"})
    assert "Нашёл не меньше 100, показываю 5" in wording.result_header(
        passport, 100, 5, capped=True
    )
    assert "Нашёл 40, показываю 5" in wording.result_header(passport, 40, 5)


def test_a_broad_request_also_gets_the_lower_bound() -> None:
    header = wording.result_header(Passport(category=Category.MOTORBIKE), 100, 5, capped=True)
    assert "(не меньше 100)" in header


def test_a_result_that_fits_the_page_is_never_called_a_lower_bound() -> None:
    assert wording.result_header(Passport(), 3, 3, capped=True) == "Вот что нашлось:"
