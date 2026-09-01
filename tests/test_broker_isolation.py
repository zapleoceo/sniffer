"""Клиент брокера не знает о базе. Проверяется импортом, а не обещанием.

Контракт «broker/client.py про базу не знает» стоял в CLAUDE.md и в докстроке
самого клиента — и всё это время нарушался: клиент импортировал
`broker/usage.py` ради значения по умолчанию, а тот тянет `db.engine` и
репозитории. Замер до правки: импорт клиента поднимал 176 модулей
`sniffer.db`/`sqlalchemy`.

Докстрока такое не ловит по своей природе. Ловит только запуск в отдельном
процессе: в общем прогоне `pytest` база уже импортирована другими тестами, и
проверка `sys.modules` показала бы её независимо от клиента.
"""

from __future__ import annotations

import subprocess
import sys

PROBE = """
import sys
import sniffer.broker.client
heavy = [m for m in sys.modules if m.startswith("sniffer.db") or m.startswith("sqlalchemy")]
print(len(heavy))
"""


def test_importing_the_broker_client_does_not_pull_the_database() -> None:
    """Отдельный процесс: иначе чужие импорты сделают тест бессмысленным."""
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", PROBE], capture_output=True, text=True, timeout=120
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "0", (
        f"импорт клиента поднял {done.stdout.strip()} модулей базы — "
        "приёмник учёта снова оказался внутри клиента"
    )
