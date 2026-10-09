"""Номера миграций не повторяются: две ветки с одним `015_*` уже встречались в волне 2.

Дубли `002_*` и `003_*` — наследие до правила, переименовать их нельзя: деплой применяет файлы по
алфавиту, а живая база уже прошла их в этом порядке. Начиная с `009` номер уникален.
"""

from __future__ import annotations

from collections import Counter

from tests.sql_chain_support import chain

LEGACY_DUPLICATES = {"002", "003"}


def test_numbers_from_009_on_are_unique() -> None:
    numbers = Counter(path.name[:3] for path in chain())
    repeated = {n for n, count in numbers.items() if count > 1}
    assert repeated <= LEGACY_DUPLICATES, f"повторяются номера: {sorted(repeated)}"


def test_migrations_14_to_22_follow_each_other_without_a_gap() -> None:
    numbers = sorted({int(path.name[:3]) for path in chain()})
    assert [n for n in numbers if n >= 14] == [14, 15, 16, 17, 18, 19, 20, 21, 22]
