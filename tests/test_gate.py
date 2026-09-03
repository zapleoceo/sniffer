"""Гейт — единственная ступень воронки, которая работает без сети и без LLM,
поэтому её поведение фиксируется тестами на реальных формулировках из чатов.
"""

from __future__ import annotations

import pytest

from sniffer.domain.passport import Category
from sniffer.pipeline.gate import gate
from sniffer.search import vocabulary

# Боевой путь: воркер внедряет в гейт детектор категорий из словаря поиска. Гейт
# сам про марки и модели больше не знает — их узнаёт этот детектор.
DETECTOR = vocabulary.category_hints

OFFERS = [
    "Продам байк Honda Vision 2022, пробег 8000, 350$, Нячанг, документы есть",
    "Сдается квартира 1 спальня в Muong Thanh, 400$/месяц, есть бассейн и зал",
    "Honda Air Blade 2021 в отличном состоянии, 7 triệu, торг уместен, район Vinh Hai",
    "For rent: studio apartment near the beach, 350 usd per month, fully furnished",
    "Cho thuê căn hộ 2 phòng ngủ, 8.500.000 VND, có máy lạnh",
]

NOT_OFFERS = [
    ("Ищу байк в аренду на месяц, бюджет до 100$", "demand_not_offer"),
    ("Всем привет, кто знает хорошего стоматолога?", "no_price_no_offer_verb"),
    ("Спасибо!", "too_short"),
    ("Продам холодильник Samsung, 200$, самовывоз", "no_category_hint"),
]


@pytest.mark.parametrize("text", OFFERS)
def test_real_offers_pass(text: str) -> None:
    result = gate(text, category_hints=DETECTOR)
    assert result.passed, f"отброшено: {result.reason}"
    assert result.categories


@pytest.mark.parametrize(("text", "reason"), NOT_OFFERS)
def test_noise_rejected(text: str, reason: str) -> None:
    result = gate(text)
    assert not result.passed
    assert result.reason == reason


def test_price_without_verb_still_passes() -> None:
    """«Honda Vision 2022, 350$» — глагола сделки нет, но это объявление.

    Категорию тут даёт не слово, а марка и модель, поэтому проходит она через
    внедрённый детектор: без него гейт узнавал бы Honda своим списком, который и
    разошёлся с рынком.
    """
    result = gate(
        "Honda Vision 2022, пробег 12000 км, 350$, Нячанг, район Loc Tho",
        category_hints=DETECTOR,
    )
    assert result.passed
    assert result.has_price
    assert not result.is_offer
    assert Category.MOTORBIKE in result.categories


def test_demand_and_offer_are_told_apart_by_order() -> None:
    """«в аренду» есть в обеих формулировках — различает только порядок."""
    assert not gate("Ищу байк в аренду на месяц, бюджет до 100$").passed
    assert gate("Сдам в аренду байк Honda, 100$/мес, ищу аккуратного арендатора").passed


def test_signals_are_serialisable() -> None:
    signals = gate(OFFERS[0]).as_signals()
    assert signals["has_price"] is True
    assert "motorbike" in signals["categories"]  # type: ignore[operator]


# Терсовое объявление, названное ТОЛЬКО именем модели: ни марки-слова, ни
# «скутер»/«байк», но цена есть — значит гейт доходит до проверки категории, а не
# отсекается ценой. До фикса каждое падало на `no_category_hint`: цена на месте,
# а категорию гейт своим списком не узнавал. Строки — класс объявлений от
# владельца, добитые до порога длины (гейт короче 25 символов не пускает вовсе).
#
# «Lead» в старом списке гейта не было ВООБЩЕ: там стояли vision, nouvo, sirius,
# winner, exciter, janus, но не lead — то есть даже Lead-объявления держались лишь
# на слове «honda», а здесь его нет. R15 и Attila не держались ни на чём.
TERSE_MODEL_ONLY = [
    "Lead 2019, 10 млн, торг, звоните",
    "Attila cũ, 8tr, chính chủ, còn đẹp",
    "R15 2019, 5tr, côn tay, máy êm",
]


@pytest.mark.parametrize("text", TERSE_MODEL_ONLY)
def test_a_model_only_ad_passes_only_with_the_injected_detector(text: str) -> None:
    """Тот самый потерянный класс лотов: марку/модель узнаёт только детектор.

    На голом дефолте гейта (одни общие слова предмета) такой лот отсеивается как
    «без категории» — это и есть довод, зачем детектор вообще внедряют.
    """
    bare = gate(text)
    assert not bare.passed
    assert bare.reason == "no_category_hint"

    injected = gate(text, category_hints=DETECTOR)
    assert injected.passed, f"отброшено: {injected.reason}"
    assert injected.categories == [Category.MOTORBIKE]


def test_the_detector_widens_category_not_the_rest_of_the_gate() -> None:
    """Детектор трогает только распознавание категории — цену и спрос не отменяет.

    Иначе «починка» тихо пропускала бы чужие запросы и объявления без цены: та же
    марка Honda Lead в них есть, но оффером они от этого не становятся.
    """
    demand = gate("Ищу байк honda lead в аренду на месяц, бюджет 500", category_hints=DETECTOR)
    assert not demand.passed
    assert demand.reason == "demand_not_offer"

    priceless = gate("Honda Lead 2019, отличное состояние, один хозяин", category_hints=DETECTOR)
    assert not priceless.passed
    assert priceless.reason == "no_price_no_offer_verb"
