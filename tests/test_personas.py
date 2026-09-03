"""Пять персон против бота: не разбор фразы, а манера человека.

Отдельно от `test_simulation.py`, потому что проверяется другое. Там сценарии
сгруппированы по предмету спора — объём, модель, город, жильё, — и каждый
держит одно свойство разбора. Здесь — по ЧЕЛОВЕКУ: одна персона пишет
несколько раз, и её манера постоянна. Дефект, который видно только на связке
«этот человек всегда пишет так», в предметной таблице не появится.

Формулировки — из журнала бота (`client_requests`, 45 запросов трёх клиентов на
03.09.2026), провенанс каждой у самой персоны в `personas.py`.

Что этот файл нашёл при первом прогоне (все три исправлены в этой же ветке):

- «Adv» — единственный текст запроса живого клиента — не узнавался вовсе:
  ни категории, ни модели, ни марки. Бот отвечал «Что ищем?» человеку, который
  предмет уже назвал;
- «Kawasaki z300» и «kawasaki z900» давали ОДИН паспорт: модель `z`, объёма
  нет. Сторона лота число из имени читала («z300» → 300), сторона клиента — нет;
- «потоцикл» и «моцокил» — опечатки в самом слове категории — теряли категорию
  целиком, и бот тратил вопрос на то, что клиент уже сказал.
"""

from __future__ import annotations

import asyncio

import pytest

from sniffer.search.intake_rules import parse_query
from sniffer.simulation.harness import Metrics, run_all
from sniffer.simulation.personas import CORRECTING, HOUSING, PERSONAS, TERSE, TYPING, VAGUE
from sniffer.simulation.script import Says
from sniffer.simulation.verdict import dialogue_faults

KEYS = tuple(scenario.key for scenario in PERSONAS)


@pytest.fixture(scope="module")
def runs() -> dict[str, Metrics]:
    """Один прогон на модуль: сеть и модель не нужны, но и лишних не надо."""
    return {metrics.scenario.key: metrics for metrics in asyncio.run(run_all(PERSONAS))}


def test_every_persona_is_covered() -> None:
    """Пять персон, и ни одна не потерялась при правке таблицы.

    Проверяется не число сценариев, а то, что каждая ГРУППА непуста: сценарий
    можно дописать, персону — нельзя молча выкинуть.
    """
    groups = {
        "терсовый": TERSE,
        "опечаточник": TYPING,
        "поправляющий": CORRECTING,
        "расплывчатый": VAGUE,
        "жилец": HOUSING,
    }

    assert len(groups) == 5
    for name, group in groups.items():
        assert group, f"персона «{name}» осталась без сценариев"
    assert len(PERSONAS) == sum(len(group) for group in groups.values())


@pytest.mark.parametrize("key", KEYS)
def test_persona_gets_what_she_asked_for(runs: dict[str, Metrics], key: str) -> None:
    """Каждая персона по отдельности — чтобы падение называло виновного поимённо."""
    faults = dialogue_faults(runs[key])

    assert not faults, f"{key}: " + "; ".join(faults)


def test_nobody_is_asked_about_what_she_already_said(runs: dict[str, Metrics]) -> None:
    """Вопрос о том, что человек уже написал, — худший из лишних.

    Потолок на ЧИСЛО вопросов проверяет `dialogue_faults` у каждой персоны
    отдельно, и там он свой: у написавшего «Kawasaki z300» город и бюджет
    неоткуда взять, а «привет» честно стоит одного вопроса — требовать от бота
    угадать предмет по приветствию значит требовать невозможного. Обобщённый
    потолок здесь был бы вторым, слабее обоснованным правилом; проверяется
    поэтому не количество, а СМЫСЛ: спрошено ли то, что уже сказано.
    """
    said_and_asked = {}
    for key, metrics in runs.items():
        fields = metrics.passport_fields
        # Поле паспорта и код вопроса зовутся одинаково («category», «city»,
        # «budget.max»), поэтому пересечение считается прямо.
        overlap = sorted(
            field
            for field in metrics.asked_fields
            if fields.get(field) not in (None, "", [], {})
            and field in _stated_by_the_first_message(metrics)
        )
        if overlap:
            said_and_asked[key] = overlap

    assert not said_and_asked, f"спрошено уже сказанное: {said_and_asked}"


def _stated_by_the_first_message(metrics: Metrics) -> frozenset[str]:
    """Поля, которые персона назвала САМА первым сообщением.

    Считается разбором той же фразы, а не памятью о сценарии: сценарий
    описывает шаги, а не ожидаемый разбор, и список «что тут названо», набранный
    руками, разошёлся бы с разбором на первой же правке словаря.
    """
    first = next((step.text for step in metrics.scenario.steps if isinstance(step, Says)), "")
    parsed = parse_query(first)
    stated: set[str] = set()
    if parsed.category is not None:
        stated.add("category")
    if parsed.city:
        stated.add("city")
    if parsed.budget.max is not None:
        stated.add("budget.max")
    for name in parsed.attributes:
        stated.add(f"attributes.{name}")
    return frozenset(stated)


def test_nobody_is_asked_the_same_thing_twice(runs: dict[str, Metrics]) -> None:
    """Повторный вопрос — признак того, что ответ не запомнился."""
    repeated = {key: m.repeated_questions for key, m in runs.items() if m.repeated_questions}

    assert not repeated, f"переспрошено: {repeated}"


def test_nobody_gets_a_stub_instead_of_help(runs: dict[str, Metrics]) -> None:
    """«Сначала напишите, что ищете» в ответ на запрос — отказ, а не ответ."""
    stubs = {key: m.stub_replies for key, m in runs.items() if m.stub_replies}

    assert not stubs, f"заглушки вместо помощи: {stubs}"


def test_the_bot_answers_every_message(runs: dict[str, Metrics]) -> None:
    """Молчание в ответ на сообщение — худший из ответов: клиент не знает, жив ли бот."""
    silent = {key: m.silent_steps for key, m in runs.items() if m.silent_steps}

    assert not silent, f"бот промолчал: {silent}"


def test_terse_client_gets_a_passport_from_one_word(runs: dict[str, Metrics]) -> None:
    """Одно слово — уже запрос, а не приветствие.

    Двое клиентов из трёх в журнале писали ТОЛЬКО название модели. Если из такого
    сообщения не выходит ни категории, ни марки, продукт для них не работает.
    """
    for scenario in TERSE:
        fields = runs[scenario.key].passport_fields
        assert fields.get("category") == "motorbike", f"{scenario.key}: категория не выведена"
        assert fields.get("attributes.model"), f"{scenario.key}: модель не узнана"
        assert fields.get("attributes.brand"), f"{scenario.key}: марка не выведена из модели"


def test_a_number_in_a_model_name_never_becomes_money(runs: dict[str, Metrics]) -> None:
    """«z300» — код модели, а не «до 300 долларов» (passport.md).

    Живой отказ 02.09.2026: клиент, искавший 300-кубовый мотоцикл, получил 50cc
    «до 300 долларов».
    """
    for scenario in TERSE + TYPING:
        fields = runs[scenario.key].passport_fields
        assert not fields.get("budget.max"), f"{scenario.key}: число прочитано деньгами"
