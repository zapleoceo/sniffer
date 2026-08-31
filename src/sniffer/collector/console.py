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


# Три попытки — не терпение, а арифметика: переспросить ничего не стоит (сети
# тут нет), а бесконечный цикл в неинтерактивной среде крутился бы вечно.
MAX_ATTEMPTS = 3


class EmptyInputError(RuntimeError):
    """Человек ничего не ввёл, а пустой ввод дальше пускать нельзя."""


def ask_required(
    console: Console,
    prompt: str,
    *,
    secret: bool = False,
    attempts: int = MAX_ATTEMPTS,
) -> str:
    """Спрашивает, пока не введут непустое.

    Пустое значение нельзя отдавать в `sign_in` ни в каком виде. Пустой код
    Telethon понимает как «кода нет»: ветка `if phone and not code and not
    password` молча шлёт ВТОРОЙ запрос кода и возвращает объект отправки
    вместо пользователя — исключения нет, а сессия остаётся неавторизованной.
    Пустой пароль двухфакторной защиты попадает в `else` того же метода и даёт
    `ValueError`. И то, и другое начинается с одного случайного Enter.
    """
    ask = console.ask_secret if secret else console.ask
    for attempts_left in range(attempts - 1, -1, -1):
        value = ask(prompt).strip()
        if value:
            return value
        if attempts_left:
            console.say("Пусто — Enter не подойдёт. Введите значение.")
    raise EmptyInputError("значение так и не ввели")
