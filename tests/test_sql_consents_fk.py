"""Миграция 016 пересоздаёт внешний ключ согласий только когда это нужно.

DROP + ADD CONSTRAINT берут блокировку на `users`; деплой гонит файл на каждом запуске, поэтому
без охраны каждый деплой ставил бы в очередь всю работу бота. Проверка статическая, без базы.
"""

from __future__ import annotations

import re

from tests.sql_chain_support import SQL_DIR

SQL = re.sub(r"--[^\n]*", "", (SQL_DIR / "016_stars_slots.sql").read_text(encoding="utf-8"))


def test_the_foreign_key_is_rebuilt_only_inside_a_guard_that_checks_cascade() -> None:
    block = re.search(r"DO \$fk\$(.*?)\$fk\$;", SQL, re.S)
    assert block is not None, "пересоздание ключа должно жить в блоке DO"
    body = block.group(1)
    assert "confdeltype = 'c'" in body and "NOT EXISTS" in body
    assert "DROP CONSTRAINT" in body and "ADD CONSTRAINT" in body
    assert "lock_timeout" in body


def test_no_unguarded_drop_of_that_constraint_remains_outside_the_block() -> None:
    outside = re.sub(r"DO \$fk\$.*?\$fk\$;", "", SQL, flags=re.S)
    assert "user_consents_user_id_fkey" not in outside
