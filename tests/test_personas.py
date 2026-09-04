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
from dataclasses import replace

import pytest

from sniffer.domain.dialogue import corrects
from sniffer.search.intake_rules import parse_query
from sniffer.simulation.harness import Metrics, run_all
from sniffer.simulation.personas import CORRECTING, HOUSING, PERSONAS, TERSE, TYPING, VAGUE
from sniffer.simulation.script import Says, Scenario, Step
from sniffer.simulation.verdict import dialogue_faults, wish_faults

KEYS = tuple(scenario.key for scenario in PERSONAS)

# ── search-first (решение владельца 04.09.2026) ─────────────────────────────
#
# `domain.dialogue.blocking_question` спрашивает до выдачи только категорию:
# город подставляется дефолтом (`intake.QueryIntake.parse` → `default_city`),
# бюджет уточняется обратной связью «дорого» под карточками. Восемь персон
# ниже родом из прежней воронки категория→город→бюджет и жмут
# `Taps("nha_trang")` / `Taps(SKIP)` ПОСЛЕ того, как категория уже понятна из
# первой же фразы — то есть отвечают на кнопки вопросов, которых search-first
# больше не задаёт.
#
# Харнес (`simulation.harness._play`) честно воспроизводит клавиатуру: код
# кнопки берётся у ПОСЛЕДНЕГО заданного вопроса. Раз вопроса про город и
# бюджет не было, «нажатие» либо попадает на код давно закрытого вопроса о
# категории — и `Conversation._answered` молча его игнорирует, ровно как
# нажатие на клавиатуру под уже устаревшими карточками
# (`test_stale_button_does_not_answer_twice` в test_bot_dialog.py), — либо
# код и вовсе пуст, и бот отвечает `NO_REQUEST_YET` («Сначала напишите, что
# ищете»). Это дефект СЦЕНАРИЯ, не бота: живой Telegram-клиент такую
# клавиатуру не показал бы вовсе, нажимать там нечего. Считать это регрессом
# значило бы требовать от search-first отвечать на кнопки, которых он сам не
# предлагал — ровно ту жалобу владельца, ради которой воронку и откатили.
#
# `simulation/personas.py` в этой задаче не в моей зоне (его ведёт отдельный
# агент), поэтому исходные шаги не правятся в источнике — только здесь, для
# прогона `runs`. Дословная формулировка персоны (первый `Says` — то, что она
# на самом деле написала) не меняется НИ В ОДНОМ сценарии; меняется только то,
# по каким кнопкам она бы жала, будь эти кнопки ещё на экране.
_SEARCH_FIRST_STEPS: dict[str, tuple[Step, ...]] = {
    "terse_z300": (Says("Kawasaki z300"),),
    "terse_cbr": (Says("Honda cbr"),),
    "terse_adv": (Says("Adv"),),
    "typo_250_minimum": (Says("нужен потоцикл 250 кубиков минимум"),),
    "typo_volume_word": (Says("обьем до 200 кубиков, скутер"),),
    "correction_cc_not_price": (
        Says("найди мне моцокил 200 кубиков. какие есть сейчас"),
        Says("не 200000 VND, а обьем мощность двигателя до 200 кубических сантиметров"),
    ),
    "vague_hello_then_scooter": (Says("привет"), Says("нужен скутер")),
    "vague_cheapest": (Says("а покажи самые дешевый скутеры которые есть на продажу"),),
}

# Категория известна из первого сообщения почти везде — вопросов до выдачи
# ноль. Исключение — «привет»: в нём нет ни одного факта, «Что ищем?» на него
# единственно верный ответ и он же единственный вопрос; следующим сообщением
# («нужен скутер») категория называется СЛОВАМИ, а не кнопкой
# (`Conversation._answer_in_words`), и выдача идёт сразу следом.
_SEARCH_FIRST_QUESTIONS: dict[str, int] = {
    "terse_z300": 0,
    "terse_cbr": 0,
    "terse_adv": 0,
    "typo_250_minimum": 0,
    "typo_volume_word": 0,
    "correction_cc_not_price": 0,
    "vague_hello_then_scooter": 1,
    "vague_cheapest": 0,
}

# Город в `correction_cc_not_price` раньше приезжал КНОПКОЙ («nha_trang») —
# вопрос под ней search-first больше не задаёт. Харнес (`simulation.harness.
# _RulesIntake`, не мой файл в этой задаче) теперь и сам подставляет
# `default_city` тем же путём, что бой (`search.intake.QueryIntake.parse`), —
# и город снова, как и раньше, доживает до конца диалога: разница только в
# ИСТОЧНИКЕ (дефолт вместо кнопки), а само регрессное свойство сценария —
# что ПОПРАВКА не должна стирать уже собранные факты — по-прежнему верно
# и для города тоже, не только для category/engine_cc.
_SEARCH_FIRST_EXPECT: dict[str, dict[str, object]] = {
    "correction_cc_not_price": {
        "category": "motorbike",
        "city": "nha_trang",
        "attributes.engine_cc": 200,
        "attributes.engine_cc_dir": "max",
    },
}


def _search_first(scenario: Scenario) -> Scenario:
    """Сценарий персоны, очищенный от кнопок воронки, которой больше нет."""
    if scenario.key not in _SEARCH_FIRST_STEPS:
        return scenario
    updates: dict[str, object] = {
        "steps": _SEARCH_FIRST_STEPS[scenario.key],
        "max_questions_before_results": _SEARCH_FIRST_QUESTIONS[scenario.key],
    }
    if scenario.key in _SEARCH_FIRST_EXPECT:
        updates["expect"] = _SEARCH_FIRST_EXPECT[scenario.key]
    return replace(scenario, **updates)  # type: ignore[arg-type]


SEARCH_FIRST_PERSONAS: tuple[Scenario, ...] = tuple(_search_first(s) for s in PERSONAS)


@pytest.fixture(scope="module")
def runs() -> dict[str, Metrics]:
    """Один прогон на модуль: сеть и модель не нужны, но и лишних не надо."""
    return {
        metrics.scenario.key: metrics for metrics in asyncio.run(run_all(SEARCH_FIRST_PERSONAS))
    }


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


def test_a_fulfilled_wish_moves_to_the_expectations(runs: dict[str, Metrics]) -> None:
    """Пожелание, которое СБЫЛОСЬ, — уже не пробел и числиться пробелом не вправе.

    Тот же страж, что у предметных сценариев, и по той же причине: пожелания
    живут под `xfail`, а он не падает никогда — значит выполненное пожелание
    остаётся зелёным, отчёт продолжает называть его несделанной работой, и
    требование так и не становится регрессной защитой.

    Этот путь пожелание поправляющего уже прошло: поправка перестала сбрасывать
    паспорт, поля переехали в `expect`, и теперь их сторожит обычный ассерт.
    """
    stale = [
        scenario.key
        for scenario in PERSONAS
        if scenario.wish is not None and not wish_faults(runs[scenario.key])
    ]

    assert not stale, f"пожелание сбылось, а числится пробелом: {stale}"


def test_a_correction_is_told_apart_from_a_new_request() -> None:
    """Что считается поправкой, а что новым запросом — на живых формулировках.

    Обе стороны важны одинаково. Пропустить поправку — сбросить собранное
    (это и был дефект). Принять новый запрос за поправку — молча продолжить
    искать прежнее, и клиент этого даже не увидит.
    """
    corrections = (
        # Из журнала бота, дословно: клиент объясняет, что 200 000 VND — не
        # бюджет, а объём двигателя.
        "не 200000 VND, а обьем мощность двигателя до 200 кубических сантиметров",
        "не скутер, а мотоцикл",
    )
    new_requests = (
        # Тоже из журнала — и это НЕ поправка: «не мотоцикл» здесь уточняет
        # предмет внутри новой просьбы, противопоставления «а» за ним нет.
        "нужен скутер, не мотоцикл, honda lead",
        "нужен мотоцикл",
        "а покажи самые дешевый скутеры которые есть на продажу",
        # Отрицания, которые поправками не являются: пропуск, цена, атрибут.
        "не важно",
        "недорого",
        "квартира не новая",
    )

    for text in corrections:
        assert corrects(text), f"поправка не узнана: {text!r}"
    for text in new_requests:
        assert not corrects(text), f"новый запрос принят за поправку: {text!r}"
