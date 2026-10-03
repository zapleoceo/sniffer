"""Запуск bash из тестов: один исполнитель для проверок shell-скриптов.

Скрипты деплоя и резервной копии проверяются запуском, а не чтением: подставной
`docker`, подставной корень файловой системы, настоящий bash. Исполнитель один,
чтобы правила запуска (см. `run`) жили в одном месте и не расходились между
файлами тестов.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass

import pytest


@dataclass(frozen=True, slots=True)
class Ran:
    """Итог запуска: потоки раздельно — тесты на утечку смотрят оба."""

    stdout: str
    stderr: str
    code: int

    @property
    def text(self) -> str:
        return self.stdout + self.stderr


def run(program: str, *, timeout: int = 120) -> Ran | None:
    """Выполнить программу в bash. `None` — bash недоступен.

    Программа уходит на stdin БАЙТАМИ: в текстовом режиме Windows превратил бы
    перевод строки в CRLF, и bash читал бы чужой скрипт с лишним символом в
    конце каждой строки.
    """
    exe = shutil.which("bash")
    if exe is None:
        return None
    try:
        done = subprocess.run(  # noqa: S603 - фиксированный argv
            [exe, "-s"],
            input=program.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return Ran(
        done.stdout.decode("utf-8", errors="replace"),
        done.stderr.decode("utf-8", errors="replace"),
        done.returncode,
    )


# Трассировка `chmod`: на любой файловой системе видно, какие права скрипт
# запросил (файловая система вправе их не хранить, как NTFS под MSYS). Строки
# `<права> <файл>` пишутся в файл из переменной TRACE; вызывающий её задаёт.
TRACE_CHMOD = 'chmod() { echo "$*" >> "$TRACE"; command chmod "$@"; }; export -f chmod'


def _bash_works() -> bool:
    done = run("echo ok")
    return done is not None and done.stdout.startswith("ok")


needs_bash = pytest.mark.skipif(not _bash_works(), reason="bash недоступен: скрипт не запустить")


def function_source(script: str, name: str) -> str:
    """Текст функции `name() { ... }` из скрипта: от объявления до `}` в начале строки."""
    pattern = "^" + re.escape(name) + r"\(\) \{\n.*?^\}\n"
    found = re.search(pattern, script, re.MULTILINE | re.DOTALL)
    assert found, f"функции {name} в скрипте нет"
    return found.group(0)
