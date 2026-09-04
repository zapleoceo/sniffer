"""Independent checks for the search-first funnel (single blocking question: category).

These assertions deliberately spell out the intended request instead of deriving
it from ``parse_query``.  A parser can consistently misread a request while the
simulation still appears internally self-consistent.

search-first (владелец, 04.09.2026): `domain.dialogue.blocking_question` спрашивает
до выдачи только категорию — город и бюджет больше не в воронке (город подставляет
`default_city` в бою, бюджет уточняет обратная связь «дорого»). Оба сценария ниже
уже переписаны под это в `sniffer/simulation/scenarios.py` (не мой файл в этой
задаче — его ведёт отдельный агент): вопросы про город/бюджет из шагов убраны,
и то, что раньше ставилось кнопкой, теперь либо названо в самом тексте запроса,
либо — раз клиент этого не сказал — попросту отсутствует в паспорте. Эти тесты
были написаны под старую (кнопочную) воронку и просто не поспевали за уже
переехавшими сценариями; правка ниже приводит ожидания в соответствие.
"""

from __future__ import annotations

import pytest

from sniffer.simulation.harness import run_scenario
from sniffer.simulation.scenarios import SCENARIOS
from sniffer.simulation.script import Scenario


def _scenario(key: str) -> Scenario:
    return next(scenario for scenario in SCENARIOS if scenario.key == key)


@pytest.mark.asyncio
async def test_plain_automatic_scooter_asks_nothing_city_defaults_budget_stays_unstated() -> None:
    """«Скутер автомат» уже называет категорию — единственный блокирующий
    вопрос закрыт самой фразой, вопросов до выдачи ноль. Город клиент не
    называл — его подставляет `default_city` (харнес разбирает через
    `intake_rules.parse_query(text, default_city=...)`, тот же путь, что и в
    бою у `search.intake.QueryIntake.parse`), а вот бюджет клиент тоже не
    называл, но кнопки, которая раньше ставила сумму без его слов, больше нет
    — бюджет остаётся честно пустым.
    """
    metrics = await run_scenario(_scenario("plain_scooter_automatic_funnel"))

    assert metrics.passport_fields["intent"] == "buy"
    assert metrics.passport_fields["category"] == "motorbike"
    assert metrics.passport_fields["attributes.transmission"] == "automatic"
    assert metrics.passport_fields["city"] == "nha_trang", "город не назван — подставлен дефолтом"
    assert "budget.max" not in metrics.passport_fields, (
        "бюджет не назван — кнопки для него больше нет"
    )
    assert metrics.asked_fields == ()
    assert not metrics.repeated_questions


@pytest.mark.asyncio
async def test_yamaha_da_nang_refinement_cuts_the_budget_without_a_question() -> None:
    """Город и марка теперь называются в самом тексте запроса (кнопок для них
    нет), а уточнение бюджета — обратной связью «дорого» под карточками, а не
    вопросом до выдачи: `Feedback.PRICEY` режет 1000 USD до 700
    (`PRICEY_FACTOR = 0.7`, `domain/dialogue.py`) без единого вопроса.
    """
    metrics = await run_scenario(_scenario("yamaha_da_nang_refinement"))

    assert metrics.passport_fields["intent"] == "buy"
    assert metrics.passport_fields["category"] == "motorbike"
    assert metrics.passport_fields["attributes.brand"] == "yamaha"
    assert metrics.passport_fields["city"] == "da_nang"
    assert metrics.passport_fields["budget.max"] == 700.0
    assert metrics.passport_fields["budget.currency"] == "USD"
    assert metrics.asked_fields == ()
    assert not metrics.repeated_questions
