"""Реестр выводов: точка расширения, на которую встаёт всё, что считается из текста.

Сегодня в реестре одно — цена. Факты жилья, район, заголовок и язык подключаются
тем же способом: класс с `name` и `derive`, одна строка в `DERIVATIONS`. Тесты
ниже держат именно этот договор, а не цену: политика цены проверена в
`test_enrich_price.py` на заданных фактах, а провод на живой разбор — в
`test_enrich_wiring.py`. Здесь цена подставляется заглушкой там, где важны числа.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sniffer.domain.listing_patch import ListingPatch
from sniffer.domain.records import Listing
from sniffer.pipeline import enrich
from sniffer.pipeline.enrich import DERIVATIONS, Derivation, DerivationFailed, derive
from sniffer.pipeline.enrich_price import FILLED, PriceDerivation
from tests.enrich_support import Parser, bounds_of, card, fact

RENT = "Oceanus, 2 спальни.\nАрендная плата: 12.5 млн VND / месяц"


def priced() -> PriceDerivation:
    """Вывод цены, которому разбор подсказывает 12,5 млн в месяц, а границы задаёт тест."""
    return PriceDerivation(parse=Parser(fact(12_500_000, period="month")), bounds_of=bounds_of)


class LangDerivation:
    """Чужой вывод: язык по алфавиту. Пишет колонку и ничего больше."""

    name = "lang"

    def derive(self, listing: Listing, text: str) -> ListingPatch:
        lang = "ru" if any("а" <= ch.lower() <= "я" for ch in text) else "en"
        changed = {"lang": lang} if listing.lang != lang else {}
        return ListingPatch(changed, outcomes=("lang.filled" if changed else "lang.same",))


class AreaDerivation:
    """Чужой вывод: свойства. Пишет только атрибуты."""

    name = "facts"

    def derive(self, listing: Listing, text: str) -> ListingPatch:
        found = {"area_m2": 30} if "30 м²" in text else {}
        return ListingPatch(attributes=found, outcomes=("facts.found" if found else "facts.none",))


class Greedy:
    """Нарушитель границы: тоже претендует на колонку цены."""

    name = "greedy"

    def derive(self, listing: Listing, text: str) -> ListingPatch:
        return ListingPatch({"price_amount": Decimal(1)}, outcomes=("greedy.took",))


class Broken:
    name = "broken"

    def derive(self, listing: Listing, text: str) -> ListingPatch:
        raise ValueError("чужой текст не разобрался: +84 90 123 45 67")


def test_the_price_is_registered_and_the_registry_derives_it() -> None:
    """Что именно найдено — не здесь: важно, что вывод цены в реестре и отвечает исходом."""
    patch = derive(card(), RENT)

    assert patch.outcomes and patch.outcomes[0].startswith("price.")


def test_every_registered_derivation_has_its_own_name() -> None:
    names = [derivation.name for derivation in DERIVATIONS]

    assert len(names) == len(set(names)), "имя — это пространство исходов в отчёте"
    assert "price" in names


# Образцы, на которых каждый зарегистрированный вывод проверяется по договору.
# Новый вывод добавляет сюда свои — договор один, и тест подхватит его сам.
SAMPLES = [
    (card(), RENT),
    (card(price=5_500), "Сдаётся 1-комн. квартира.\nЦена: 5,5 млн VND/мес"),
    (card(category="motorbike"), "Honda Vision в аренду, 250 000 в сутки"),
    (card(price=9_000_000), "Продам Honda Vision 2019"),
]


@pytest.mark.parametrize("derivation", DERIVATIONS, ids=lambda d: d.name)
@pytest.mark.parametrize(("listing", "text"), SAMPLES)
def test_every_registered_derivation_keeps_the_contract(
    derivation: Derivation, listing: Listing, text: str
) -> None:
    """Исход есть всегда и назван по выводу; повторный проход по результату пуст."""
    patch = derivation.derive(listing, text)

    assert patch.outcomes, "вывод без исхода невидим в отчёте"
    assert all(o.startswith(f"{derivation.name}.") for o in patch.outcomes)
    assert derivation.derive(patch.applied_to(listing), text).is_empty, "идемпотентность"


def test_a_new_derivation_joins_by_one_line_in_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Точка расширения: дописали в `DERIVATIONS` — и вывод работает, ничего не правя."""
    monkeypatch.setattr(enrich, "DERIVATIONS", (priced(), LangDerivation(), AreaDerivation()))

    patch = derive(card(), RENT + "\nПлощадь 30 м²")

    assert patch.columns["price_amount"] == Decimal(12_500_000)
    assert patch.columns["lang"] == "ru"
    assert patch.attributes["area_m2"] == 30
    assert patch.outcomes == (FILLED, "lang.filled", "facts.found")


def test_the_registry_can_also_be_passed_explicitly() -> None:
    patch = derive(card(), RENT, derivations=(LangDerivation(),))

    assert dict(patch.columns) == {"lang": "ru"}, "цена не считалась: её нет в переданном списке"


def test_a_derivation_cannot_take_what_another_one_already_claimed() -> None:
    with pytest.raises(DerivationFailed) as caught:
        derive(card(), RENT, derivations=(priced(), Greedy()))

    assert caught.value.derivation == "greedy"
    assert type(caught.value.__cause__).__name__ == "PatchClash"


def test_a_failing_derivation_is_named_and_its_cause_is_kept() -> None:
    with pytest.raises(DerivationFailed) as caught:
        derive(card(), RENT, derivations=(priced(), Broken()))

    assert caught.value.derivation == "broken"
    assert isinstance(caught.value.__cause__, ValueError)


def test_the_failure_message_carries_the_name_and_never_the_text() -> None:
    with pytest.raises(DerivationFailed) as caught:
        derive(card(), RENT, derivations=(Broken(),))

    assert "broken" in str(caught.value)
    assert "+84" not in str(caught.value), "текст чужого исключения не переносится"


def test_an_empty_registry_derives_nothing() -> None:
    patch = derive(card(), RENT, derivations=())

    assert patch.is_empty and patch.outcomes == ()
