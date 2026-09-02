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
«ещё не сделано».
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


def test_a_known_category_is_never_paid_for_with_a_question(runs: dict[str, Metrics]) -> None:
    """Категория названа — значит план поиска собрать есть из чего, и мы ищем.

    Это перевёрнутое правило владельца из passport.md: показать выдачу рано и
    уточнять обратной связью. Вопрос вместо помощи и есть та «тупизна», на
    которую он жаловался.
    """
    guilty = {
        key: metrics.asked_fields
        for key, metrics in runs.items()
        if metrics.scenario.max_questions_before_results == 0
        and metrics.questions_before_results > 0
    }

    assert not guilty, f"вопросы до выдачи там, где категория известна: {guilty}"


def test_no_dialogue_ever_asks_more_than_one_question_before_results(
    runs: dict[str, Metrics],
) -> None:
    """Потолок абсолютный: без категории — один вопрос, и это максимум для всех."""
    over = {
        key: metrics.questions_before_results
        for key, metrics in runs.items()
        if metrics.questions_before_results > 1
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


def test_feedback_is_the_place_for_questions(runs: dict[str, Metrics]) -> None:
    """Вопрос после нажатой кнопки клиент запросил сам — до выдачи он лишний."""
    for key in ("pricey_after_results", "wrong_after_results"):
        metrics = runs[key]
        assert metrics.questions_before_results == 0, f"{key}: спросил до выдачи"
        assert metrics.reached_results, f"{key}: выдачи не было вовсе"


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


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Живой поиск ранжирует, но категорию не отсекает: чужая категория из "
        "телеграм-чата проходит в пятёрку по свежести. Порог отсечения вводится "
        "соседней задачей (search/relevance.py), поэтому здесь не strict — тест "
        "позеленеет сам, когда отсев доедет, и не потребует правки"
    ),
)
def test_the_cards_shown_all_fit_the_request(runs: dict[str, Metrics]) -> None:
    dirty = {
        key: relevance_faults(metrics)[0] for key, metrics in runs.items() if metrics.off_target
    }

    assert not dirty, f"мимо запроса: {dirty}"


@pytest.mark.parametrize("key", [scenario.key for scenario in SCENARIOS if scenario.wish])
@pytest.mark.xfail(
    strict=False,
    reason=(
        "Пожелания сценариев — это требования, которые пока не выполнены "
        "(разбор коробки в первичном запросе, вывод категории из модели). "
        "Причина каждого записана в самом сценарии (`Wish.why`). Не strict, "
        "потому что часть из них закрывается прямо сейчас соседней задачей: "
        "strict превратил бы чужой успех в наше падение"
    ),
)
def test_known_gaps_are_still_gaps(runs: dict[str, Metrics], key: str) -> None:
    unmet = wish_faults(runs[key])

    assert not unmet, f"{key}: " + "; ".join(unmet)


def _only_filled[T](rows: Mapping[str, tuple[T, ...]]) -> dict[str, tuple[T, ...]]:
    """Только виноватые сценарии: в сообщении об ошибке нужны они, а не все два десятка."""
    return {key: value for key, value in rows.items() if value}
