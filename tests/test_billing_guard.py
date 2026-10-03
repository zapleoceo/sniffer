"""Охрана денежных сценариев: любой исход документирован, а не трейсбек.

Правило CLAUDE.md («Как закрывают набор кодов возврата»): полноту доказывает не список
ожидаемых классов исключений, а охрана до КОРНЯ иерархии и тест с чужим типом по обеим осям —
`Exception` и `BaseException`. Структурные проверки стоят первыми: у прерывания тест не просто
краснеет, а обрывает весь прогон pytest, и внятное красное должно успеть появиться до обрыва.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from sniffer.bot import billing_guard
from sniffer.bot.billing_guard import Flow, is_interrupt, let_exit_through
from tests.billing_support import Failing

SRC = Path(__file__).resolve().parents[1] / "src" / "sniffer"
MONEY = [*SRC.glob("bot/billing*.py"), SRC / "bot" / "handlers" / "billing.py"]


def _handlers(path: Path) -> list[ast.ExceptHandler]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]


def _name(handler: ast.ExceptHandler) -> str:
    return "" if handler.type is None else ast.unparse(handler.type)


# ── структура: сначала то, что не может оборвать прогон ─────────────────────


def test_there_is_no_except_exception_and_no_bare_except_near_the_money() -> None:
    """`Exception` — не корень иерархии: мимо него идут Ctrl+C, снятая задача и группа."""
    offenders = [
        f"{path.name}: except {_name(handler) or '<голый>'}"
        for path in MONEY
        for handler in _handlers(path)
        if _name(handler) in {"", "Exception"}
    ]

    assert MONEY and not offenders, offenders


def test_the_root_of_the_hierarchy_is_guarded_in_exactly_two_documented_places() -> None:
    """Охрана до корня — одна на сценарий (`Flow.step`) и одна на окно `pre_checkout`.

    Третье место — это шаг, который выбирает сам, что ловить; а выбор по памяти автора и есть
    тот список, который правило запрещает.
    """
    where = {
        path.name: sum(1 for handler in _handlers(path) if _name(handler) == "BaseException")
        for path in MONEY
    }

    assert {name: count for name, count in where.items() if count} == {
        "billing_guard.py": 1,
        "billing_service.py": 1,
    }


def test_every_other_except_near_the_money_is_specific() -> None:
    """Любой другой `except` рядом с деньгами — конкретный и осмысленный, а не «на всякий»."""
    allowed = {"BaseException", "TelegramAPIError", "(OverflowError, OSError, ValueError)"}
    odd = [
        f"{path.name}: except {_name(handler)}"
        for path in MONEY
        for handler in _handlers(path)
        if _name(handler) not in allowed
    ]

    assert not odd, odd


# ── отличие прерывания от поломки ───────────────────────────────────────────


@pytest.mark.parametrize(
    "error",
    [
        KeyboardInterrupt(),
        asyncio.CancelledError(),
        BaseExceptionGroup("группа", [KeyboardInterrupt()]),
        BaseExceptionGroup("группа", [asyncio.CancelledError(), KeyboardInterrupt()]),
        BaseExceptionGroup("вложенная", [BaseExceptionGroup("внутри", [KeyboardInterrupt()])]),
    ],
)
def test_an_interrupt_is_recognised_alone_and_inside_a_group(error: BaseException) -> None:
    assert is_interrupt(error)


@pytest.mark.parametrize(
    "error",
    [
        Failing("сбой"),
        ValueError("сбой"),
        BaseExceptionGroup("смешанная", [asyncio.CancelledError(), Failing("настоящий сбой")]),
        ExceptionGroup("группа сбоев", [Failing("сбой")]),
    ],
)
def test_a_failure_is_not_an_interrupt_even_next_to_one(error: BaseException) -> None:
    """Смешанная группа — про сбой: иначе «прервано» стало бы свалкой для чужих ошибок."""
    assert not is_interrupt(error)


@pytest.mark.parametrize("error", [SystemExit(3), GeneratorExit()])
def test_a_request_to_exit_is_passed_through_not_rewritten(error: BaseException) -> None:
    with pytest.raises(type(error)):
        let_exit_through(error)


@pytest.mark.parametrize("error", [Failing("x"), KeyboardInterrupt(), asyncio.CancelledError()])
def test_everything_else_is_not_let_through_by_the_exit_guard(error: BaseException) -> None:
    let_exit_through(error)


# ── шаг сценария ────────────────────────────────────────────────────────────


async def _ok() -> str:
    return "готово"


def _raising(error: BaseException) -> Callable[[], Awaitable[str]]:
    async def work() -> str:
        raise error

    return work


async def test_a_step_returns_its_value() -> None:
    flow = Flow("t")

    done = await flow.step("ok", _ok)

    assert done.ok and done.value == "готово"
    flow.finish()


async def test_a_foreign_failure_becomes_an_outcome_not_a_traceback() -> None:
    flow = Flow("t")

    done = await flow.step("boom", _raising(Failing("чужое")))

    assert not done.ok and isinstance(done.error, Failing)
    assert done.or_else("запасное") == "запасное"
    flow.finish()


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(KeyboardInterrupt, id="KeyboardInterrupt"),
        pytest.param(asyncio.CancelledError, id="CancelledError"),
        pytest.param(lambda: BaseExceptionGroup("g", [KeyboardInterrupt()]), id="group"),
    ],
)
async def test_an_interrupt_does_not_stop_the_scenario_but_is_raised_when_it_is_done(
    make: Callable[[], BaseException],
) -> None:
    """Сценарий доделывает документированное (сообщает владельцу) и только потом уступает."""
    flow = Flow("t")
    interrupted = await flow.step("interrupted", _raising(make()))
    later = await flow.step("later", _ok)

    assert not interrupted.ok and later.ok, "следующий шаг выполнился"
    with pytest.raises((KeyboardInterrupt, asyncio.CancelledError, BaseExceptionGroup)):
        flow.finish()


async def test_a_mixed_group_is_reported_as_a_failure_and_not_raised() -> None:
    flow = Flow("t")
    mixed = BaseExceptionGroup("g", [asyncio.CancelledError(), Failing("настоящий")])

    done = await flow.step("mixed", _raising(mixed))

    assert not done.ok
    flow.finish()


async def test_the_first_interrupt_is_the_one_that_is_raised() -> None:
    flow = Flow("t")
    await flow.step("a", _raising(KeyboardInterrupt("первое")))
    await flow.step("b", _raising(asyncio.CancelledError()))

    with pytest.raises(KeyboardInterrupt, match="первое"):
        flow.finish()


@pytest.mark.parametrize("error", [SystemExit(3), GeneratorExit()])
async def test_a_step_never_swallows_a_request_to_exit(error: BaseException) -> None:
    flow = Flow("t")

    with pytest.raises(type(error)):
        await flow.step("exit", _raising(error))


async def test_a_pure_function_is_guarded_too() -> None:
    """Платёж уже снят: баг в чистой функции не должен оставить его без записи."""
    flow = Flow("t")

    def broken() -> int:
        raise Failing("баг")

    done = await flow.compute("pure", broken)
    fine = await flow.compute("pure", lambda: 7)

    assert not done.ok and fine.value == 7


def test_the_guard_module_names_where_the_root_is_caught() -> None:
    """Если охрана переехала — переедет и этот тест: список мест закрыт."""
    assert "except BaseException" in Path(billing_guard.__file__).read_text(encoding="utf-8")


# ── ни одного вызова порта вне охраняемого шага ─────────────────────────────

SERVICES = [SRC / "bot" / name for name in ("billing_service.py", "billing_payments.py")]
SERVICES.append(SRC / "bot" / "billing_support.py")
SERVICES.append(SRC / "bot" / "billing_reconcile.py")
# Единственный метод, где порты зовутся прямо: его целиком вызывает `Flow.step` через lambda.
DIRECT_CALLERS = {"_issue"}


def unguarded_port_calls(source: str) -> list[str]:
    """`self._ledger.x(...)` и `self._api.x(...)` вне lambda, переданной в `Flow.step`."""
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    found = []
    for node in ast.walk(tree):
        inner = node.value if isinstance(node, ast.Attribute) else None
        if not (
            isinstance(inner, ast.Attribute)
            and isinstance(inner.value, ast.Name)
            and inner.value.id == "self"
            and inner.attr in {"_ledger", "_api", "_slots"}
        ):
            continue
        current: ast.AST = node
        guarded, owner = False, "<модуль>"
        while current in parents:
            current = parents[current]
            if isinstance(current, ast.Lambda):
                guarded = True
                break
            if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
                owner = current.name
                break
        if not guarded and owner not in DIRECT_CALLERS:
            found.append(f"{inner.attr}.{getattr(node, 'attr', '?')} в {owner}")
    return found


@pytest.mark.parametrize("path", SERVICES, ids=lambda path: path.name)
def test_no_port_is_called_outside_a_guarded_step(path: Path) -> None:
    """«Ни одной работающей строки вне охраняемого блока»: вызов порта — шаг, а шаг охраняется."""
    assert not unguarded_port_calls(path.read_text(encoding="utf-8"))


def test_the_checker_itself_sees_a_bare_port_call() -> None:
    """Проверяющий не должен быть всеядным: иначе тест выше ничего не доказывает."""
    bare = "class D:\n    async def go(self):\n        await self._ledger.record_payment(1)\n"
    wrapped = (
        "class D:\n    async def go(self, flow):\n"
        "        await flow.step('x', lambda: self._ledger.record_payment(1))\n"
    )

    assert unguarded_port_calls(bare) == ["_ledger.record_payment в go"]
    assert unguarded_port_calls(wrapped) == []


def test_the_one_direct_caller_is_reached_only_through_a_guarded_step() -> None:
    """`_issue` зовёт порты прямо; это допустимо, пока сам он вызывается только из `Flow.step`."""
    source = (SRC / "bot" / "billing_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    uses = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in DIRECT_CALLERS
    ]

    assert uses, "прямой вызывающий больше не используется — уберите его из DIRECT_CALLERS"
    for node in uses:
        ancestors = []
        current: ast.AST = node
        while current in parents:
            current = parents[current]
            ancestors.append(current)
        assert any(isinstance(a, ast.Lambda) for a in ancestors), f"строка {node.lineno}"
