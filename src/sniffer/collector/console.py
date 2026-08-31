"""Разговор команды `auth` с человеком за терминалом.

Отдельным модулем по двум причинам. Во-первых, это единственная часть, которую
тест обязан подменить целиком: живой `input()` в pytest — это зависший прогон.
Во-вторых, здесь держится правило вывода — **stdout отдан машиночитаемому
результату, всё для человека идёт в stderr**. Смешать их значит подмешать
подсказки в то, что вызывающий разбирает как путь к файлу.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from getpass import getpass


class NoTerminalError(RuntimeError):
    """Спросить код не у кого: ввод не подключён к терминалу."""


def _say(text: str) -> None:
    print(text, file=sys.stderr)


def _ask(prompt: str) -> str:
    print(prompt, file=sys.stderr, end="", flush=True)
    try:
        return input().strip()
    except EOFError as err:
        # Страховка на случай, если терминал исчез уже после проверки.
        raise NoTerminalError("ввод закончился, кода нет") from err


def _stdin_is_a_terminal() -> bool:
    return sys.stdin.isatty()


@dataclass(slots=True, frozen=True)
class Console:
    """Ввод-вывод команды одним объектом — чтобы тест не трогал терминал."""

    say: Callable[[str], None] = _say
    ask: Callable[[str], str] = _ask
    # getpass сам печатает приглашение в stderr и гасит эхо: пароль
    # двухфакторной защиты не должен остаться в истории терминала.
    ask_secret: Callable[[str], str] = getpass
    # Спрашивается ДО отправки кода: Telegram прислал бы его, а прочитать было
    # бы некому — и следующая попытка упёрлась бы в FloodWait.
    is_interactive: Callable[[], bool] = _stdin_is_a_terminal
