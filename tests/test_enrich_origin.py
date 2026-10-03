"""Пара «категория, сторона», под которой воронка прочла бы текст при создании.

Нужна только отчёту: он помечает записи, которые объясняются сменой стороны или
категории после вердикта модели. Правила — воронки (`category_hints`,
`parse_query`, `offer_deal_type`); здесь проверено, что их взяли оттуда, а не
скопировали: глагол сделки, предмет, умолчание категории. Сама пометка
(`changed_by_verdict`) проверена на подставных ответах: логика «воспроизводит ли
чтение под прежней парой то, что лежит в карточке» не зависит от живого разбора.
"""

from __future__ import annotations

from decimal import Decimal

from sniffer.domain.records import Listing
from sniffer.worker.enrich_origin import changed_by_verdict, funnel_view
from tests.enrich_support import card

RENT = "Сдам 1-комнатную квартиру у моря, 9 млн в месяц"
SELL = "Продам Honda Vision 2019, 21 млн"


def test_a_rental_text_is_read_as_a_rental_of_an_apartment() -> None:
    assert funnel_view(card(), RENT) == ("apartment", "rent_out")


def test_the_view_does_not_depend_on_what_the_card_says_now() -> None:
    """Вердикт мог сменить сторону — а воронка читает ТЕКСТ, а не карточку."""
    flipped = card(deal_type="sell")

    assert funnel_view(flipped, RENT) == ("apartment", "rent_out")


def test_the_category_is_the_first_subject_named_in_the_text() -> None:
    wrong = card(category="apartment", deal_type="rent_out")

    assert funnel_view(wrong, SELL) == ("motorbike", "sell")


def test_without_a_verb_of_the_deal_the_default_of_the_category_decides() -> None:
    """Жильё сдают, технику продают (`default_deal_type`)."""
    assert funnel_view(card(), "Квартира у моря, 2 спальни")[1] == "rent_out"
    assert funnel_view(card(category="motorbike"), "Honda Vision 2019")[1] == "sell"


def test_a_verb_of_the_deal_beats_the_default_of_the_category() -> None:
    """Жильё по умолчанию сдают, технику продают — но глагол в тексте сильнее."""
    assert funnel_view(card(), "Продам квартиру, 5 млрд")[1] == "sell"
    assert funnel_view(card(category="motorbike"), "Сдам Honda Vision в аренду")[1] == "rent_out"


def test_a_text_that_names_no_subject_falls_back_to_the_category_of_the_card() -> None:
    assert funnel_view(card(category="bicycle"), "Срочно, недорого")[0] == "bicycle"


def test_an_unknown_category_of_the_card_does_not_break_the_view() -> None:
    assert funnel_view(card(category="неизвестная"), "Срочно, недорого")[0] == "неизвестная"


# ── пометка: правка объясняется сменой пары ────────────────────────────────


class Calls:
    """Подставные `funnel` и `read`: отдают заданное и помнят, о чём их спросили."""

    def __init__(self, origin: tuple[str, str], read: Decimal | None) -> None:
        self.origin, self.result = origin, read
        self.read_with: list[tuple[str, str, str]] = []

    def funnel(self, listing: Listing, text: str) -> tuple[str, str]:
        return self.origin

    def read(self, text: str, category: str, deal_type: str) -> Decimal | None:
        self.read_with.append((text, category, deal_type))
        return self.result


def verdict(listing: Listing, calls: Calls) -> bool:
    return changed_by_verdict(listing, "текст", funnel=calls.funnel, read=calls.read)


def test_a_card_whose_pair_never_changed_has_nothing_to_explain() -> None:
    calls = Calls(("apartment", "rent_out"), None)

    assert verdict(card(deal_type="rent_out"), calls) is False
    assert calls.read_with == [], "и текст под прежней парой не читаем"


def test_a_change_is_explained_when_the_old_pair_reproduces_what_the_card_holds() -> None:
    """Под арендой цены нет, и в карточке цены нет: пропуск — следствие смены стороны."""
    calls = Calls(("apartment", "rent_out"), None)

    assert verdict(card(deal_type="sell", price=None), calls) is True


def test_a_stored_price_the_old_pair_reproduces_is_explained_too() -> None:
    """13 млн аренды, лежащие в цене продажи: прежняя сторона читает ровно их."""
    calls = Calls(("apartment", "rent_out"), Decimal(13_000_000))

    assert verdict(card(deal_type="sell", price=13_000_000), calls) is True


def test_a_change_is_not_explained_when_the_old_pair_would_read_something_else() -> None:
    """Пара сменилась, но «5500» дал прежний разбор, а не сторона: смена ни при чём."""
    calls = Calls(("apartment", "rent_out"), Decimal(5_500_000))

    assert verdict(card(deal_type="sell", price=5_500), calls) is False


def test_the_text_is_read_under_the_pair_of_the_funnel_not_the_pair_of_the_card() -> None:
    calls = Calls(("house", "rent_out"), None)

    verdict(card(category="apartment", deal_type="sell"), calls)

    assert calls.read_with == [("текст", "house", "rent_out")]


def test_with_the_real_funnel_a_price_missed_under_rent_is_explained_by_the_flip_to_sale() -> None:
    text = "Квартира в Нячанге.\nЦена: 4 390 000 000 VND"

    flipped = card(category="apartment", deal_type="sell")
    stable = card(category="apartment", deal_type="rent_out")

    assert changed_by_verdict(flipped, text) is True, "под арендой 4,39 млрд — не цена"
    assert changed_by_verdict(stable, text) is False, "пара та же, что дала бы воронка"
