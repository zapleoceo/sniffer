"""Провод: настоящий разбор цены, настоящая таблица границ, настоящая воронка.

Единственное место, где тесты прохода зависят от живого разбора и от таблицы границ
(`domain/prices.py`, `domain/price_bounds.py`) — обе правятся в ветке цены. Остальные
тесты проверяют политику на заданных фактах и заданных границах. Если после слияния с
веткой цены краснеет что-то, то только здесь (и в одной живой проверке проводки,
`test_db_enrichment.py::test_the_pass_with_every_real_default_reads_a_label_and_fills_the_price`),
и вопрос в одном: изменился разбор или границы так, что пример ниже перестал быть примером.
"""

from __future__ import annotations

from decimal import Decimal

from sniffer.pipeline.enrich import derive
from sniffer.pipeline.enrich_price import ABSENT, FILLED, REPLACED, PriceDerivation, derive_price
from sniffer.worker.enrich_origin import changed_by_verdict
from tests.enrich_support import card

LABELLED_RENT = "Oceanus, 2 спальни.\nАрендная плата: 12.5 млн VND / месяц"
# Сумма, которую границы аренды отвергают, а границы продажи принимают.
BILLIONS = "Квартира в Нячанге.\nЦена: 4 390 000 000 VND"


def test_the_registered_derivation_reads_a_labelled_rent_with_the_real_parser() -> None:
    patch = PriceDerivation().derive(card(), LABELLED_RENT)

    assert patch.outcomes == (FILLED,)
    assert patch.columns["price_amount"] == Decimal(12_500_000)


def test_the_registry_derives_the_price_by_default() -> None:
    assert derive(card(), LABELLED_RENT).outcomes == (FILLED,)


def test_the_real_parser_is_asked_about_the_current_side_of_the_card() -> None:
    as_sale = PriceDerivation().derive(card(deal_type="sell"), BILLIONS)
    as_rent = PriceDerivation().derive(card(deal_type="rent_out"), BILLIONS)

    assert as_sale.outcomes == (FILLED,), "под продажей это цена"
    assert as_rent.outcomes == (ABSENT,), "под арендой 4,39 млрд — не цена"


def test_with_the_real_funnel_a_price_missed_under_rent_is_explained_by_the_flip_to_sale() -> None:
    flipped = card(category="apartment", deal_type="sell")
    stable = card(category="apartment", deal_type="rent_out")

    assert changed_by_verdict(flipped, BILLIONS) is True, "под арендой 4,39 млрд — не цена"
    assert changed_by_verdict(stable, BILLIONS) is False, "пара та же, что дала бы воронка"


def test_by_default_the_old_price_is_judged_by_the_real_bounds_table() -> None:
    """«5500» вместо «5,5 млн»: вне границ аренды по боевой таблице, и метка её заменяет."""
    text = "Сдаётся 1-комн. квартира.\nЦена: 5,5 млн VND/мес"

    by_class = PriceDerivation().derive(card(price=5_500), text)
    by_function = derive_price(card(price=5_500), text)

    assert by_class.outcomes == (REPLACED,)
    assert by_function.outcomes == (REPLACED,)


AN_HOME_STUDIO = (
    "AN-HOME | аренда начинается здесь…\n🌿 Студия в Центре Нячанга 🌴\n📍 Локация:\n"
    "• Центр Нячанга\n• ЖК The Sanhome 1\n🟢 О квартире:\n• Студия\n• Площадь: 26,3 м²\n"
    "🟡 Условия аренды:\n• Цена: 1.5 млн VND / месяц\n"
)


def test_a_studio_at_one_and_a_half_million_is_a_leftover_and_the_pass_erases_it() -> None:
    """Живой диалог 04.10.2026: «1 500 000» у AN-HOME ниже пола аренды квартиры (2 млн).

    Нынешний разбор такую цену из текста не отдаёт, значит в базе она осталась от прежнего
    разбора, и проход догона (`enrich`) её стирает — а не оставляет как «известную цену».
    """
    from sniffer.pipeline.enrich_price import ERASED

    patch = derive_price(card(price=1_500_000), AN_HOME_STUDIO)

    assert patch.outcomes == (ERASED,)
    assert patch.columns["price_amount"] is None
