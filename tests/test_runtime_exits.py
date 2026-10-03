"""Что разовая команда обязана знать об исключениях одинаково на каждом шаге.

Три вещи, и каждая однажды стоила круга ревью (`collector/auth.py`, CLAUDE.md,
«Как закрывают набор кодов возврата»): просьба выйти не поломка, прерывание
бывает ГРУППОЙ, а текст исключения базы нельзя печатать.
"""

from __future__ import annotations

import asyncio

import pytest

from sniffer.runtime.exits import class_chain, is_interrupt, reraise_if_not_ours


class Boom(Exception):
    """Чужой тип: код его не знает и не перечисляет."""


@pytest.mark.parametrize("request_to_stop", [SystemExit(3), GeneratorExit()])
def test_a_request_to_stop_is_passed_on_not_rewritten(request_to_stop: BaseException) -> None:
    with pytest.raises(type(request_to_stop)) as caught:
        reraise_if_not_ours(request_to_stop)

    assert caught.value is request_to_stop, "тот же объект: чужой код выхода своим не переписать"


@pytest.mark.parametrize(
    "failure",
    [Boom("x"), KeyboardInterrupt(), asyncio.CancelledError(), BaseExceptionGroup("g", [Boom()])],
)
def test_everything_else_is_left_for_the_caller_to_answer(failure: BaseException) -> None:
    reraise_if_not_ours(failure)  # не бросила — значит, шаг ответит сам


@pytest.mark.parametrize(
    "interrupt",
    [
        KeyboardInterrupt(),
        asyncio.CancelledError(),
        BaseExceptionGroup("g", [KeyboardInterrupt(), asyncio.CancelledError()]),
        BaseExceptionGroup("outer", [BaseExceptionGroup("inner", [KeyboardInterrupt()])]),
    ],
)
def test_an_interrupt_may_arrive_as_a_group_of_interrupts(interrupt: BaseException) -> None:
    assert is_interrupt(interrupt) is True


@pytest.mark.parametrize(
    "failure",
    [
        Boom("x"),
        OSError("диск"),
        # Рядом со снятой задачей приехал настоящий сбой: ответ обязан быть про сбой.
        BaseExceptionGroup("g", [KeyboardInterrupt(), Boom()]),
        ExceptionGroup("g", [Boom()]),
    ],
)
def test_a_failure_is_not_an_interrupt_even_beside_one(failure: BaseException) -> None:
    assert is_interrupt(failure) is False


def test_the_class_chain_names_the_cause_and_never_the_text() -> None:
    secret = "INSERT … title='Сдам квартиру, +84 90 123 45 67 @owner_name'"
    try:
        try:
            raise Boom(secret)
        except Boom as inner:
            raise RuntimeError(secret) from inner
    except RuntimeError as outer:
        chain = class_chain(outer)

    assert chain == "RuntimeError <- Boom"
    assert "+84" not in chain and "@owner_name" not in chain


def test_the_class_chain_follows_the_orig_attribute_of_database_errors() -> None:
    """SQLAlchemy прячет причину в `orig`, а не в `__cause__`."""

    class DbError(Exception):
        def __init__(self, orig: BaseException) -> None:
            super().__init__("(параметры SQL с текстом объявления)")
            self.orig = orig

    assert class_chain(DbError(Boom("x"))) == "DbError <- Boom"


def test_the_class_chain_survives_a_cycle() -> None:
    first, second = Boom("a"), RuntimeError("b")
    first.__cause__, second.__cause__ = second, first

    assert class_chain(first) == "Boom <- RuntimeError"


def test_the_class_chain_is_bounded() -> None:
    err: BaseException = Boom("0")
    for _ in range(20):
        nxt = RuntimeError("n")
        nxt.__cause__ = err
        err = nxt

    assert len(class_chain(err).split(" <- ")) == 4


def test_a_group_names_its_members_one_level_deep() -> None:
    group = ExceptionGroup("g", [Boom("x"), OSError("y")])

    assert class_chain(group) == "ExceptionGroup[Boom, OSError]"


def test_a_broken_str_of_an_exception_cannot_break_the_description() -> None:
    class Hostile(Exception):
        def __str__(self) -> str:
            raise RuntimeError("str() сломан")

    assert class_chain(Hostile()) == "Hostile"
