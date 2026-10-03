"""Логика оплаты не знает ни базы, ни aiogram. Проверяется импортом, а не обещанием.

Весь смысл портов (`billing_ports.py`) в том, что правила и сервисы оплаты проверяются без
Postgres и без Telegram. Это свойство исчезает тихо: достаточно одного импорта «ради
удобства», и тест денег снова требует базу или сеть. Поэтому его держит запуск в отдельном
процессе, как `test_broker_isolation.py` и `test_wording_isolation.py`: в общем прогоне
`pytest` база и aiogram уже импортированы соседними тестами, и проверка `sys.modules`
показала бы их независимо от модуля.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# Модули, где решаются деньги и тексты. Адаптеры (`billing_ledger`, `billing_telegram`,
# `billing_ui`) и роутер сюда не входят: им знать базу и aiogram — работа.
LOGIC = (
    "sniffer.bot.billing",
    "sniffer.bot.billing_wording",
    "sniffer.bot.billing_owner_wording",
    "sniffer.bot.billing_ports",
    "sniffer.bot.billing_guard",
    "sniffer.bot.billing_service",
    "sniffer.bot.billing_payments",
    "sniffer.bot.billing_support",
)

PROBE = """
import importlib
import sys
importlib.import_module(sys.argv[1])
heavy = sorted(
    m for m in sys.modules
    if m.startswith(("sniffer.db", "sqlalchemy", "aiogram", "telethon"))
)
print(len(heavy), heavy[:3])
"""


@pytest.mark.parametrize("module", LOGIC)
def test_importing_the_payment_logic_does_not_pull_the_database_or_telegram(module: str) -> None:
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", PROBE, module], capture_output=True, text=True, timeout=120
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("0 "), (
        f"{module} поднял модули базы или Telegram: {done.stdout.strip()} — "
        "деньги снова нельзя проверить без них"
    )
