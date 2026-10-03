"""Тесты с живой базой не зашивают «сейчас»: у базы свои часы.

Окно архива, срок подписки и свежесть сверяются в SQL с `now()` самой базы, а
не с тем временем, которое тест передал в Python. Зашитое `NOW = datetime(2026,
8, 31, ...)` с `until=NOW + 30 суток` истекло само 30.09.2026, и два теста
покраснели без единой правки кода. CI этого не видел, потому что красным он стал
в день, когда никто ничего не пушил, и узнали бы по первому же пушу, а деплой
зависит от `quality`.

Класс дефекта один и возвращается с каждым новым тестом, поэтому его держит не
договорённость, а проверка: зашитое «сейчас» в модуле с живой базой не проходит.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).parent
LIVE_DB = ("TEST_DATABASE_URL", "db_engine", "db_session")


def hardcoded_now(path: Path) -> list[str]:
    """Модульные `NOW = datetime(<константы>)`: дата, которая со временем протухнет."""
    found = []
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.Assign):
            continue
        names = {target.id for target in node.targets if isinstance(target, ast.Name)}
        value = node.value
        if (
            "NOW" in names
            and isinstance(value, ast.Call)
            and getattr(value.func, "id", "") == "datetime"
            and all(isinstance(arg, ast.Constant) for arg in value.args)
        ):
            found.append(f"{path.name}:{node.lineno}")
    return found


def test_live_database_tests_do_not_hardcode_now() -> None:
    modules = [
        path
        for path in sorted(TESTS.glob("test_*.py"))
        if path.name != Path(__file__).name
        and any(marker in path.read_text(encoding="utf-8") for marker in LIVE_DB)
    ]
    offenders = [place for path in modules for place in hardcoded_now(path)]

    assert modules, "ни одного модуля с живой базой: проверка не смотрит на то, что должна"
    assert not offenders, (
        f"зашитое «сейчас» в тестах с живой базой: {offenders}. База сверяет сроки и "
        "окна со своим `now()`: берите `datetime.now(UTC)`"
    )
