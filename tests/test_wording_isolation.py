"""Формулировки бота не знают о базе и Telegram. Проверяется импортом.

Весь смысл выноса текстов из `conversation.py` в том, что их можно проверить
без Postgres и без aiogram. Это свойство исчезает тихо: достаточно одного
импорта «ради удобства», и тест формулировки снова требует базу. Поэтому его
держит запуск в отдельном процессе, как `test_broker_isolation.py`: в общем
прогоне `pytest` база уже импортирована соседними тестами, и проверка
`sys.modules` показала бы её независимо от модуля.
"""

from __future__ import annotations

import subprocess
import sys

PROBE = """
import sys
import sniffer.bot.wording
heavy = [
    m for m in sys.modules
    if m.startswith(("sniffer.db", "sqlalchemy", "aiogram", "telethon"))
]
print(len(heavy))
"""


def test_importing_the_wording_does_not_pull_the_database_or_telegram() -> None:
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", PROBE], capture_output=True, text=True, timeout=120
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "0", (
        f"импорт формулировок поднял {done.stdout.strip()} модулей базы или Telegram — "
        "тексты снова нельзя проверить без них"
    )
