"""Суждение о прогоне: что именно здесь плохо и почему.

Отделено от сбора фактов намеренно. Факты снимает `harness.py`, и они не
устаревают; устаревают правила, по которым мы называем диалог умным. Держи их
в одном файле с прогоном — и любая правка требования означала бы правку кода,
который водит бота.

Два набора, и смешивать их нельзя:

* **дефекты диалога** — лишний вопрос, потерянное поле, молчание, заглушка
  вместо помощи. Это регресс: вчера работало, сегодня нет, и в CI такое обязано
  быть красным.
* **дефекты выдачи** — показанное противоречит запросу. Это системное свойство
  живого поиска, а не регресс: `rank_items` отсекает чужой ПРЕДМЕТ (категорию и
  модель), а свойства — коробку, марку, цену — только опускает баллом, потому
  что продавец не обязан их называть. Красить этим весь прогон значило бы
  утопить сигнал о регрессе в постоянном красном.
"""

from __future__ import annotations

from sniffer.simulation.harness import Metrics


def faults(metrics: Metrics) -> tuple[str, ...]:
    return dialogue_faults(metrics) + relevance_faults(metrics)


def dialogue_faults(metrics: Metrics) -> tuple[str, ...]:
    """Что бот сделал не так в РАЗГОВОРЕ."""
    scenario = metrics.scenario
    found: list[str] = []

    limit = scenario.max_questions_before_results
    if metrics.questions_before_results > limit:
        found.append(
            f"вопросов до выдачи {metrics.questions_before_results} при потолке {limit}: "
            f"{', '.join(metrics.asked_fields) or '—'}"
        )
    if metrics.repeated_questions:
        found.append(f"переспросил про {', '.join(metrics.repeated_questions)}")
    # Сравнение с `True`/`False` поимённо, а не по истинности: `None` означает
    # «всё равно», и падать он не должен ни на одной из двух веток.
    if scenario.expect_results is True and not metrics.reached_results:
        found.append("до карточек не дошёл")
    if scenario.expect_results is False and metrics.reached_results:
        found.append("показал карточки там, где верный ответ — отказ по делу")
    if metrics.silent_steps:
        steps = ", ".join(str(number) for number in metrics.silent_steps)
        found.append(f"промолчал на шаг {steps}")
    if metrics.stub_replies:
        found.append(f"ответил заглушкой: «{metrics.stub_replies[0][:40]}…»")
    if scenario.expect_text and not any(scenario.expect_text in text for text in metrics.replies):
        found.append(f"в ответах нет «{scenario.expect_text}»")

    found += _field_faults(metrics)
    return tuple(found)


def relevance_faults(metrics: Metrics) -> tuple[str, ...]:
    """Что бот показал мимо запроса. Судится правдой о лоте, а не оценкой ранжировщика."""
    if not metrics.off_target:
        return ()
    # Счёт по показам, перечень по лотам: один и тот же мусор в двух выдачах
    # подряд — две ошибки, но читать его дважды незачем.
    seen = dict.fromkeys(f"{card.external_id} — {card.reason}" for card in metrics.off_target)
    return (f"мимо запроса {len(metrics.off_target)} из {metrics.cards_shown}: {'; '.join(seen)}",)


def wish_faults(metrics: Metrics) -> tuple[str, ...]:
    """Невыполненные пожелания — известные пробелы, а не регрессы.

    Отдельная функция, потому что и место у них отдельное: в тестах под
    `xfail`, в отчёте — строкой «известный пробел» с причиной. Смешай их с
    дефектами — и красный перестанет означать «сломалось».
    """
    wish = metrics.scenario.wish
    if wish is None:
        return ()
    return tuple(
        message
        for path, value in wish.fields.items()
        if (message := _compare(metrics, path, value))
    )


def _field_faults(metrics: Metrics) -> list[str]:
    faults_found = [
        message
        for path, value in metrics.scenario.expect.items()
        if (message := _compare(metrics, path, value))
    ]
    for path in metrics.scenario.forbid:
        if path in metrics.passport_fields:
            faults_found.append(
                f"{path} заполнено ({metrics.passport_fields[path]!r}), а должно быть пустым"
            )
    return faults_found


def _compare(metrics: Metrics, path: str, expected: object) -> str:
    actual = metrics.passport_fields.get(path)
    if actual is None:
        return f"{path} не распозналось, ожидалось {expected!r}"
    if not _same(actual, expected):
        return f"{path} = {actual!r}, ожидалось {expected!r}"
    return ""


def _same(actual: object, expected: object) -> bool:
    """Числа сравниваются числами: 300 из кнопки и 300.0 из разбора — одна сумма."""
    if isinstance(actual, int | float) and isinstance(expected, int | float):
        return float(actual) == float(expected)
    return actual == expected
