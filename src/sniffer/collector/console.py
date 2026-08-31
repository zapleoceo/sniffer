"""Разговор команды `auth` с человеком за терминалом.

Отдельным модулем по двум причинам. Во-первых, это единственная часть, которую
тест обязан подменить целиком: живой `input()` в pytest — это зависший прогон.
Во-вторых, здесь держится правило вывода — **stdout отдан машиночитаемому
результату, всё для человека идёт в stderr**. Смешать их значит подмешать
подсказки в то, что вызывающий разбирает как путь к файлу.

Из второго правила следует то, что легко потерять: **если stderr нет, текст для
человека не пишется никуда**. `print(text, file=None)` уходит в stdout, а
`sys.stderr` у процесса с закрытым fd 2 равен ровно `None` — то есть подсказки
молча полились бы в машиночитаемый канал.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from getpass import getpass
from typing import TextIO


class NoTerminalError(RuntimeError):
    """Спросить код не у кого: ввод не подключён к терминалу."""


def _human_stream() -> TextIO | None:
    """stderr, если он есть. Подмены на stdout здесь быть не может."""
    stream = sys.stderr
    return stream if stream is not None and not getattr(stream, "closed", False) else None


def _say(text: str) -> None:
    stream = _human_stream()
    if stream is None:
        # Некуда — значит никуда: в stdout лежит путь к файлу с сессией, и
        # подмешивать в него подсказки для человека нельзя.
        return
    print(text, file=stream)


def _ask(prompt: str) -> str:
    stream = _human_stream()
    if stream is not None:
        print(prompt, file=stream, end="", flush=True)
    try:
        return input().strip()
    except (EOFError, RuntimeError, OSError) as err:
        # Три разных способа потерять терминал уже после проверки, и все три
        # означают одно и то же — спросить не у кого:
        # EOFError — ввод закончился (`</dev/null`, отвалившийся pty);
        # RuntimeError — `input(): lost sys.stdout` у процесса с закрытым fd 1
        #   (`input()` печатает приглашение сам и без stdout работать не умеет);
        # OSError — fd закрыли из-под уже созданного объекта потока (EBADF).
        # Без этой обёртки два последних улетали в `except Exception`
        # («сбой протокола MTProto») и в `except OSError` («сеть недоступна») —
        # оба совета уводят владельца искать поломку не там.
        raise NoTerminalError(f"ввод недоступен ({type(err).__name__}: {err})") from err


def _ask_secret(prompt: str) -> str:
    """`getpass` сам печатает приглашение в stderr и гасит эхо.

    Обёртка нужна ровно из-за конца ввода: голый `getpass` на EOF бросает
    `EOFError`, который мимо `NoTerminalError` уходит в общий обработчик, и
    потеря терминала на приглашении 2ФА давала rc=7 («сбой протокола MTProto»)
    вместо rc=6 того же события на приглашении кода.
    """
    try:
        return getpass(prompt)
    except (EOFError, RuntimeError, OSError) as err:
        raise NoTerminalError(f"ввод пароля недоступен ({type(err).__name__}: {err})") from err


def _console_is_usable() -> bool:
    """Терминал на вводе есть И оба потока живы.

    `stdin.isatty()` в одиночку не отвечает на вопрос «сможем ли мы вообще
    поговорить». `python -m sniffer.collector auth 1>&- 2>&-` оставляет
    терминал на stdin, но `sys.stdout` и `sys.stderr` у такого процесса равны
    `None`, и `input()` падает `RuntimeError: lost sys.stdout` — уже ПОСЛЕ
    `send_code_request`, то есть код подтверждения потрачен, а следующая
    попытка рискует FloodWait.

    Всеведения тут по-прежнему нет: в отсоединённой сессии `tmux` терминал
    формально есть, а человека за ним нет (об этом сказано в `deploy.md`).
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is None or getattr(stream, "closed", False):
            return False
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except (OSError, ValueError):
        # fd закрыт из-под живого объекта потока: сам объект есть, писать некуда.
        return False
    return sys.stdin.isatty()


@dataclass(slots=True, frozen=True)
class Console:
    """Ввод-вывод команды одним объектом — чтобы тест не трогал терминал."""

    say: Callable[[str], None] = _say
    ask: Callable[[str], str] = _ask
    # Пароль двухфакторной защиты не должен остаться в истории терминала.
    ask_secret: Callable[[str], str] = _ask_secret
    # Спрашивается ДО отправки кода: Telegram прислал бы его, а прочитать было
    # бы некому — и следующая попытка упёрлась бы в FloodWait.
    is_interactive: Callable[[], bool] = _console_is_usable


def tell(console: Console, text: str) -> None:
    """Сказать человеку — не повод изменить исход команды.

    Единственная дверь, через которую вывод для человека покидает команду.
    Причина в том, что этот вывод физически может отказать: на общей машине
    докеровский `json-file` пишет в тот же том, и когда он переполняется,
    stderr отвечает `OSError(ENOSPC)`. Прямой `console.say` в обработчике
    ошибки бросал бы ВТОРОЕ исключение изнутри первого — наружу летел rc=1 с
    трейсбеком и без единой строки диагностики, то есть ровно там, где
    диагностика и была нужна.

    Проглатывается только `OSError`: это «канал не работает». Всё остальное —
    наш баг в форматировании строки, и прятать его не за что.
    """
    try:
        console.say(text)
    except OSError:
        pass


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
        # Пробелы обрезаются ТОЛЬКО у кода: он цифровой, и краевой пробел из
        # копипасты — заведомо мусор. Облачный пароль пробелом может и
        # начинаться, и заканчиваться; обрезав его, мы молча портим то, что
        # человек ввёл верно, и Telegram отвечает rc=3 «пароль не тот» —
        # владелец идёт искать ошибку в пароле, которого не портил.
        raw = ask(prompt)
        value = raw if secret else raw.strip()
        if value:
            return value
        if attempts_left:
            tell(console, "Пусто — Enter не подойдёт. Введите значение.")
    raise EmptyInputError("значение так и не ввели")
