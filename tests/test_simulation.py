"""Симуляция диалога: инварианты качества, а не отдельные случаи.

Юнит-тесты диалога проверяют по случаю на тест и каждый по отдельности остаётся
зелёным ровно до того дня, когда набор случаев В СУММЕ превращается в допрос.
Здесь проверяется сумма: два десятка живых формулировок рынка Нячанга гоняются
через настоящий `Conversation`, и утверждения касаются всего набора сразу —
«ни один сценарий с известной категорией не задаёт вопроса до выдачи», «объём
двигателя никогда не становится бюджетом».

Инварианты сформулированы по НАБЛЮДАЕМОМУ поведению (`Reply`, ссылки в
карточках, сохранённый паспорт), а не по внутренностям `search/` и `domain/`:
эти модули сейчас переписываются, и тест, заглядывающий внутрь, краснел бы от
чужой правки, ничего не сообщая о качестве разговора.

Что здесь `xfail` и почему — в конце файла. Коротко: невыполненное ожидание
удаляют либо признают. Удалить значит потерять требование, поэтому оно
остаётся, но отдельно от регрессов: красный обязан означать «сломалось», а не
«ещё не сделано». Обратный ход обязателен и не бесплатен: выполненное ожидание
переезжает в обычный ассерт, иначе оно навсегда останется зелёным и ничего не
сторожащим — `xfail` не падает никогда.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from sniffer.domain.passport import Category, Passport
from sniffer.simulation.catalog import CATALOG
from sniffer.simulation.fit import off_target
from sniffer.simulation.harness import Metrics, run_all, run_scenario
from sniffer.simulation.report import render_replies, render_report
from sniffer.simulation.scenarios import SCENARIOS
from sniffer.simulation.script import Says, Scenario
from sniffer.simulation.verdict import dialogue_faults, relevance_faults, wish_faults

KEYS = tuple(scenario.key for scenario in SCENARIOS)


@pytest.fixture(scope="session")
def runs() -> dict[str, Metrics]:
    """Все сценарии прогоняются один раз на сессию: они чистые и повторять их незачем."""
    return {metrics.scenario.key: metrics for metrics in asyncio.run(run_all())}


# ── инварианты качества: утверждения обо ВСЁМ наборе ────────────────────────


def test_each_dialogue_asks_only_for_fields_missing_from_the_request(
    runs: dict[str, Metrics],
) -> None:
    """Сценарий задаёт потолок, равный числу действительно недостающих полей."""
    guilty = {
        key: metrics.asked_fields
        for key, metrics in runs.items()
        if metrics.questions_before_results > metrics.scenario.max_questions_before_results
    }

    assert not guilty, f"лишние вопросы до выдачи: {guilty}"


def test_no_dialogue_ever_asks_more_than_three_questions_before_results(
    runs: dict[str, Metrics],
) -> None:
    """Абсолютный потолок: предмет, город и бюджет — новых анкет не строим."""
    over = {
        key: metrics.questions_before_results
        for key, metrics in runs.items()
        if metrics.questions_before_results > 3
    }

    assert not over, f"допрос до выдачи: {over}"


def test_a_question_is_never_asked_twice(runs: dict[str, Metrics]) -> None:
    """Переспросить о том же — самый заметный способ выглядеть сломанным."""
    repeated = {key: metrics.repeated_questions for key, metrics in runs.items()}

    assert not any(repeated.values()), f"повторные вопросы: {_only_filled(repeated)}"


def test_the_bot_answers_every_message(runs: dict[str, Metrics]) -> None:
    """Молчание в ответ хуже неверного ответа: клиент не знает, дошло ли вообще."""
    silent = {key: metrics.silent_steps for key, metrics in runs.items()}

    assert not any(silent.values()), f"бот промолчал: {_only_filled(silent)}"


def test_nobody_gets_a_stub_instead_of_help(runs: dict[str, Metrics]) -> None:
    """«Сначала напишите, что ищете» в ответ на запрос — это отказ, а не ответ."""
    stubs = {key: metrics.stub_replies for key, metrics in runs.items()}

    assert not any(stubs.values()), f"заглушки вместо помощи: {_only_filled(stubs)}"


@pytest.mark.parametrize("key", KEYS)
def test_scenario_meets_its_expectations(runs: dict[str, Metrics], key: str) -> None:
    """Каждый сценарий по отдельности — чтобы падение называло виновного поимённо."""
    faults = dialogue_faults(runs[key])

    assert not faults, f"{key}: " + "; ".join(faults)


# ── кубики, которые не деньги ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "найди мне моцокил 200 кубиков",
        "не 200000 VND, а обьем мощность двигателя до 200 кубических сантиметров",
    ],
    ids=["первая_формулировка", "поправка_клиента"],
)
async def test_engine_displacement_never_becomes_a_budget(text: str) -> None:
    """Живой отказ 01.09.2026, стоивший клиента дважды.

    Обе фразы взяты из лога дословно: сначала «200 кубиков» стали бюджетом в
    200000 VND (семь долларов), потом клиент поправил вслух — и получил тот же
    бюджет второй раз. Проверяется отдельным тестом, а не строкой в таблице
    сценариев: требование пережило две правки разбора и обязано пережить
    третью.
    """
    metrics = await run_scenario(
        Scenario(
            key="engine_cc",
            title=text,
            steps=(Says(text),),
            max_questions_before_results=1,
            expect_results=None,
        )
    )

    assert metrics.passport_fields.get("attributes.engine_cc") == 200
    assert "budget.max" not in metrics.passport_fields, (
        f"объём двигателя уехал в бюджет: {metrics.passport_fields}"
    )


# ── честный отказ там, где искать нечем ─────────────────────────────────────


def test_an_unserved_city_gets_an_answer_and_not_a_search(runs: dict[str, Metrics]) -> None:
    """Хойан не ищем — и говорим об этом, а не показываем нячангскую выдачу."""
    metrics = runs["unserved_city"]

    assert not metrics.reached_results, "искать там, где нет источников, нечем"
    assert metrics.questions_before_results == 0, "уточнять бюджет в чужом городе — впустую"
    assert any("Хойан" in text for text in metrics.replies)


def test_feedback_starts_only_after_the_search_funnel(runs: dict[str, Metrics]) -> None:
    assert runs["pricey_after_results"].questions_before_results == 0
    assert runs["wrong_after_results"].questions_before_results == 1
    assert runs["pricey_after_results"].reached_results
    assert runs["wrong_after_results"].reached_results


# ── харнес меряет бота, а не себя ───────────────────────────────────────────


def test_the_market_keeps_lots_that_do_not_fit() -> None:
    """Каталог обязан содержать заведомо неподходящее — иначе метрика мертва.

    Подделка, отдающая только идеальные совпадения, показала бы стопроцентную
    релевантность при любом коде: мерился бы каталог, а не выдача. Тест
    сторожит саму возможность измерить — включая тот случай, когда каталог
    «почистят», чтобы отчёт выглядел лучше.
    """
    lead = Passport(category=Category.MOTORBIKE, attributes={"brand": "honda", "model": "lead"})
    reasons = {lot.item.external_id: off_target(lot, lead) for lot in CATALOG}

    assert any("чужая модель" in reason for reason in reasons.values()), "нет близкого промаха"
    assert any("чужая категория" in reason for reason in reasons.values()), "нет чужой категории"
    assert any(lot.item.posted_at is not None for lot in CATALOG)
    assert sum(1 for reason in reasons.values() if not reason) >= 2, "нет ни одного попадания"


def test_the_report_names_every_scenario(runs: dict[str, Metrics]) -> None:
    """Запускаемый отчёт — такая же поставка, как тесты, и ломается так же молча."""
    text = render_report(list(runs.values()))

    for scenario in SCENARIOS:
        assert scenario.title[:30] in text or scenario.key in text, scenario.key
    assert "сценариев:" in text
    assert render_replies(runs["scooter_nha_trang"]).count("\n") > 1


# ── известные пробелы: не удалены, но и не выданы за регресс ────────────────


def test_the_cards_shown_all_fit_the_request(runs: dict[str, Metrics]) -> None:
    dirty = {
        key: relevance_faults(metrics)[0] for key, metrics in runs.items() if metrics.off_target
    }

    assert not dirty, f"мимо запроса: {dirty}"


def test_a_fulfilled_wish_moves_to_the_expectations(runs: dict[str, Metrics]) -> None:
    """Пожелание, которое СБЫЛОСЬ, — уже не пробел, и числиться пробелом не вправе.

    Пожелания стояли под `xfail(strict=False)`, чтобы работа соседней задачи не
    красила прогон в красный. Плата за это — молчание в обратную сторону:
    выполненное пожелание остаётся зелёным, отчёт продолжает называть его
    несделанной работой, а требование так и не становится регрессной защитой,
    потому что `xfail` не падает никогда.

    Поэтому проверяется именно переезд: сбылось — переноси поля в `expect`, где
    их сторожит обычный ассерт (`test_scenario_meets_its_expectations`). Оба
    пожелания замера 02.09.2026 этот путь уже прошли, и список сейчас пуст.
    """
    stale = [
        scenario.key
        for scenario in SCENARIOS
        if scenario.wish is not None and not wish_faults(runs[scenario.key])
    ]

    assert not stale, f"пожелание сбылось, а числится пробелом: {stale} — переносите в expect"


def _only_filled[T](rows: Mapping[str, tuple[T, ...]]) -> dict[str, tuple[T, ...]]:
    """Только виноватые сценарии: в сообщении об ошибке нужны они, а не все два десятка."""
    return {key: value for key, value in rows.items() if value}
