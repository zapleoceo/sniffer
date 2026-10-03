"""Охрана денежных сценариев: любой сбой превращается в документированный исход, а не в трейсбек.

Правило CLAUDE.md («Как закрывают набор кодов возврата»), применённое к платежам. Список
ожидаемых классов исключений не доказывает полноты — он доказывает только то, что
вспомнили; поэтому каждый шаг, который делает работу, стоит внутри блока, чей последний
`except` — `BaseException`, а не `Exception`: `Exception` не корень иерархии, и мимо него
идут `KeyboardInterrupt`, `CancelledError` и `BaseExceptionGroup`.

Из корня перебрасываются ровно две вещи, и они не поломки, а просьба остановиться:
`SystemExit` и `GeneratorExit` (`let_exit_through`, ОДНА функция на модуль, чтобы шаг не
выбирал сам). Прерывание отличает тоже одна функция (`is_interrupt`): оно не глотается —
сценарий доделывает документированное (сообщает владельцу) и поднимает его наверх,
потому что снятая задача, которая «выжила», ломает остановку процесса.

Здесь живёт единственный `except BaseException` денежного кода, кроме охраны окна
`pre_checkout` (`billing_service.guarded_verdict`): `tests/test_billing_guard.py`
следит, что другого `except Exception` или `except BaseException` рядом с платежами нет.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from sniffer.bot.billing_ports import BotApiError

log = structlog.get_logger(__name__)


def let_exit_through(exc: BaseException) -> None:
    """`SystemExit` и `GeneratorExit` не наши: чужой код выхода своим не переписываем."""
    if isinstance(exc, SystemExit | GeneratorExit):
        raise exc


def is_interrupt(exc: BaseException) -> bool:
    """Прервали ли работу: Ctrl+C, снятая задача — или ГРУППА только из таких.

    Смешанная группа (снятая задача рядом с настоящим сбоем) прерыванием не считается:
    ответ обязан быть про сбой, иначе «прервано» становится свалкой для чужих ошибок.
    """
    if isinstance(exc, KeyboardInterrupt | asyncio.CancelledError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return all(is_interrupt(sub) for sub in exc.exceptions)
    return False


def describe(exc: BaseException) -> str:
    """Как назвать сбой в сообщении владельцу, не вынося наружу лишнего.

    Описание от Telegram — это текст ошибки Bot API и токена не содержит; у остальных
    исключений текста нет вовсе: в сообщениях драйверов бывают адреса и параметры.
    """
    if isinstance(exc, BotApiError):
        return f"{type(exc).__name__}: {str(exc)[:200]}"
    return type(exc).__name__


@dataclass(frozen=True, slots=True)
class Attempt[T]:
    """Итог одного шага: значение либо пойманный сбой."""

    value: T | None = None
    error: BaseException | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def or_else(self, default: T) -> T:
        return default if self.error is not None or self.value is None else self.value


class Flow:
    """Один денежный сценарий: шаги охраняются до корня, прерывание поднимается в конце."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._interrupt: BaseException | None = None

    async def step[T](self, step: str, work: Callable[[], Awaitable[T]]) -> Attempt[T]:
        """Выполнить шаг. Работа передаётся вызываемой, а не готовым объектом: так и сам
        вызов (разбор аргументов, создание корутины) оказывается внутри охраны."""
        try:
            return Attempt(value=await work())
        except BaseException as exc:
            let_exit_through(exc)
            log.error("billing.step_failed", flow=self._name, step=step, error=type(exc).__name__)
            if is_interrupt(exc) and self._interrupt is None:
                self._interrupt = exc
            return Attempt(error=exc)

    async def compute[T](self, step: str, fn: Callable[[], T]) -> Attempt[T]:
        """Тот же шаг для чистой функции: она тоже может оказаться с багом, а платёж уже снят."""

        async def run() -> T:
            return fn()

        return await self.step(step, run)

    def finish(self) -> None:
        """Сценарий доделан; если по дороге прервали — прерывание идёт дальше."""
        if self._interrupt is not None:
            raise self._interrupt
