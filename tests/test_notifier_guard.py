"""Набор исходов отправки закрыт: что бы ни вылетело на шаге, исход документирован.

Правило из CLAUDE.md («Как закрывают набор кодов возврата»): перечисление классов
исключений не доказывает полноту, оно доказывает только то, что вспомнили. Поэтому
на каждый шаг отправки подкладывается чужой `Boom(Exception)`, а отдельно — все
виды прерывания, и проверяется исход. Список шагов в тесте связан с кодом
механически: число шагов под охраной сверяется с исходником (`ast`), так что
седьмой шаг без строки в матрице красит тест, а не остаётся незамеченным.

Документированные исходы:

* шаг отправки (сборка текста, Bot API) — Boom → повтор с паузой, попытка засчитана;
* прерывание (Ctrl+C, отмена, группа с ними) — пробрасывается, из прерванной
  отправки в базе не остаётся ничего: строка ждёт, как ждала;
* отказ базы — пробрасывается, процесс падает и стартует заново: молча идти дальше
  с недописанной очередью нельзя.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap

import pytest

from sniffer.notifier import delivery as module
from sniffer.notifier.delivery import Delivery
from tests.notifier_support import Boom, Clock, Row, Store, Telegram, deliver

# Шаги, которые `Delivery._attempt` исполняет под одной охраной. Добавили шаг в
# `try` — допишите сюда, иначе `test_every_step_under_the_guard_is_in_the_matrix` красный.
GUARDED_STEPS = ("render", "send")

# Шаги базы на пути успеха: (имя, сколько вызовов пропустить). Второй `commit` — тот,
# что стоит после отметки «отправлено»; первый — уборка очереди перед выборкой.
DATABASE_STEPS = [
    ("take_pending", 0),
    ("cancel_for_blocked_users", 0),
    ("cancel_expired", 0),
    ("lock_pending", 0),
    ("mark_sent", 0),
    ("commit", 1),
]

INTERRUPTS = [
    KeyboardInterrupt(),
    asyncio.CancelledError(),
    BaseExceptionGroup("только прерывания", [KeyboardInterrupt()]),
    BaseExceptionGroup("смешанная", [Boom(), asyncio.CancelledError()]),
]
INTERRUPT_IDS = ["ctrl_c", "cancelled", "group_of_interrupts", "mixed_group"]


def broken(
    step: str, error: BaseException, monkeypatch: pytest.MonkeyPatch
) -> tuple[Store, Delivery]:
    """Очередь из двух клиентов и нотифаер, у которого на шаге `step` вылетает `error`."""
    store, clock = Store([Row(1), Row(2, user_id=8, recipient_id=43)]), Clock()
    if step == "render":

        def explode(_messages: object) -> str:
            raise error

        monkeypatch.setattr(module, "_text", explode)
        return store, Delivery(Telegram(store, clock), pause_s=0.0, clock=clock, scope=store.scope)
    if step == "send":
        return store, deliver(store, clock, error)[0]
    store.fail(step, error)
    return store, deliver(store, clock)[0]


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instantly(_seconds: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", instantly)


@pytest.mark.parametrize("step", GUARDED_STEPS)
@pytest.mark.parametrize(
    "error", [Boom("чужой"), ExceptionGroup("группа", [Boom()])], ids=["boom", "group"]
)
async def test_every_step_answers_with_a_documented_outcome_for_an_unknown_failure(
    step: str, error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, delivery = broken(step, error, monkeypatch)

    await delivery.tick()  # исключение, вышедшее из прохода, было бы трейсбеком, а не исходом

    row = store.row(1)
    assert (row.status, row.attempts) == ("pending", 1), "документированный исход — повтор позже"
    assert row.last_error and type(error).__name__ in row.last_error


@pytest.mark.parametrize("step", GUARDED_STEPS)
@pytest.mark.parametrize("interrupt", INTERRUPTS, ids=INTERRUPT_IDS)
async def test_an_interrupt_at_a_guarded_step_is_never_swallowed(
    step: str, interrupt: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, delivery = broken(step, interrupt, monkeypatch)

    with pytest.raises(type(interrupt)):
        await delivery.tick()

    assert (store.row(1).status, store.row(1).attempts) == ("pending", 0)
    assert store.row(2).status == "pending", "после прерывания проход не продолжается"


@pytest.mark.parametrize(("step", "skip"), DATABASE_STEPS)
@pytest.mark.parametrize("error", [Boom("база"), *INTERRUPTS], ids=["boom", *INTERRUPT_IDS])
async def test_a_database_failure_stops_the_pass_and_leaves_the_rest_untouched(
    step: str, skip: int, error: BaseException
) -> None:
    store, clock = Store([Row(1), Row(2, user_id=8, recipient_id=43)]), Clock()
    store.fail(step, error, after=skip)
    delivery, _ = deliver(store, clock)

    with pytest.raises(type(error)):
        await delivery.tick()

    assert store.row(2).status == "pending", "очередь осталась нетронутой: нечего терять"
    if step in ("mark_sent", "commit"):
        assert store.row(1).status == "pending", (
            "незакоммиченное не видно: повторится ≤ 1 сообщение"
        )


# ── устройство охраны, а не только её поведение ─────────────────────────────


def _guard() -> ast.Try:
    source = textwrap.dedent(inspect.getsource(Delivery._attempt))
    tries = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Try)]
    assert len(tries) == 1, "под охраной должен быть один блок: два — это два набора исходов"
    return tries[0]


def test_the_guard_is_rooted_at_base_exception() -> None:
    """`Exception` — не корень: Ctrl+C и отмена задачи мимо него дают трейсбек."""
    (handler,) = _guard().handlers

    assert isinstance(handler.type, ast.Name) and handler.type.id == "BaseException"
    assert "except Exception" not in inspect.getsource(Delivery._attempt)


def test_every_step_under_the_guard_is_in_the_matrix() -> None:
    """Список шагов в тесте — тоже список того, что вспомнили. Сверяем с исходником."""
    steps = [node for node in _guard().body if not isinstance(node, ast.Pass)]

    assert len(steps) == len(GUARDED_STEPS), (
        "число шагов под охраной изменилось: допишите шаг в GUARDED_STEPS и в `broken`"
    )


def test_no_step_chooses_what_to_propagate_by_itself() -> None:
    """Перебрасывание — одна функция на модуль (`outcome.must_propagate`).

    Если бы `delivery.py` называл прерывания сам, на одном шаге их забыли бы: дописанное
    вручную «и ещё вот этот класс» — тот же список, просто в другом месте.
    """
    names = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(ast.parse(inspect.getsource(module)))
        if isinstance(node, ast.Name | ast.Attribute)
    }

    assert not names & {"KeyboardInterrupt", "CancelledError", "SystemExit", "GeneratorExit"}
