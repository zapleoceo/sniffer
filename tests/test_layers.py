"""Направление зависимостей между слоями. Проверяется импортами, а не обещанием.

CLAUDE.md называет порядок и добавляет: «Обратных зависимостей нет, `domain` не
зависит ни от чего». До этого теста правило держалось на внимательности — и не
удержалось: `db/repositories/listings.py` начал импортировать `matching`, то
есть слой, который сам его и вызывает. Правка заняла минуту, потому что её
поймали сразу; через месяц это был бы цикл импортов и рефакторинг на день.

Запрещённое перечисляется, а не разрешённое, и это осознанно. Список
разрешённых рёбер устаревает на каждой новой связи и превращается в тест,
который правят под код. Список запрещённых — это само правило CLAUDE.md,
переписанное дословно: он меняется только вместе с решением о слоях.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).parents[1] / "src" / "sniffer"

# Кто кого не смеет импортировать. Ключ — пакет, значение — запрещённые ему.
FORBIDDEN: dict[str, frozenset[str]] = {
    # Ядро предметной области. Не зависит ни от чего своего вообще — иначе
    # его нельзя взять в тест или в утилиту без половины проекта.
    "domain": frozenset(
        {
            "bot",
            "broker",
            "collector",
            "dashboard",
            "db",
            "matching",
            "notifier",
            "pipeline",
            "runtime",
            "search",
            "sources",
            "verifier",
            "worker",
        }
    ),
    # Хранилище. Знает про `domain` и больше ни про кого: всё остальное
    # обращается к нему, а не наоборот.
    "db": frozenset(
        {
            "bot",
            "collector",
            "dashboard",
            "matching",
            "notifier",
            "pipeline",
            "search",
            "sources",
            "verifier",
            "worker",
        }
    ),
    # Отбор и оценка. Читает через `db`, но ничего не знает ни про источники,
    # ни про диалог: иначе подписки нельзя посчитать без Telegram.
    "matching": frozenset({"bot", "collector", "dashboard", "notifier", "sources", "search"}),
    # Воронка идёт в одну сторону: сырьё → проверка → карточка.
    "pipeline": frozenset({"bot", "collector", "dashboard", "notifier", "search", "sources"}),
    # Проверяльщик не знает ни про диалог, ни про воронку, которая его зовёт.
    "verifier": frozenset({"bot", "collector", "dashboard", "notifier", "pipeline"}),
}

# Единственное разрешённое исключение, и оно названо в CLAUDE.md поимённо:
# адаптеру `telegram_groups` нужен реестр чатов, и знание о `db` сведено в одну
# функцию. Всё остальное в `sources` про базу не знает.
SOURCES_DB_GATE = "chat_directory.py"


def _imports(path: pathlib.Path) -> set[str]:
    """Какие пакеты `sniffer` импортирует этот файл."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    packages: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sniffer."):
            packages.add((node.module or "").split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("sniffer."):
                    packages.add(alias.name.split(".")[1])
    return packages


def _modules(package: str) -> list[pathlib.Path]:
    return sorted((SRC / package).rglob("*.py"))


@pytest.mark.parametrize("package", sorted(FORBIDDEN))
def test_a_layer_never_imports_the_layers_above_it(package: str) -> None:
    banned = FORBIDDEN[package]
    for path in _modules(package):
        crossed = _imports(path) & banned
        assert not crossed, (
            f"{path.relative_to(SRC.parent.parent)} импортирует {sorted(crossed)} — "
            f"это обратная зависимость, а их в проекте нет (CLAUDE.md)"
        )


def test_sources_reach_the_database_through_one_named_door() -> None:
    """Знание `sources` о базе сведено в одну функцию, и так и остаётся.

    CLAUDE.md: «Новое обращение к `db` из `sources` мимо этой границы —
    основание переделать, а не дописать импорт». Тест и есть эта граница.
    """
    offenders = [
        path.name
        for path in _modules("sources")
        if "db" in _imports(path) and path.name != SOURCES_DB_GATE
    ]

    assert not offenders, (
        f"{offenders} ходят в базу мимо {SOURCES_DB_GATE} — "
        "адаптер источника обязан получать реестр снаружи, а не искать его сам"
    )


def test_every_package_of_the_project_is_judged_or_named() -> None:
    """Новый пакет не проскакивает мимо проверки молча.

    Иначе тест защищает ровно те слои, которые вспомнили в день его написания,
    — та же ошибка, что перечислять классы исключений вместо охраны до корня.
    """
    known = set(FORBIDDEN) | {
        # Точки входа процессов и общий каркас: своей предметной логики у них
        # нет, они собирают чужую, поэтому импортируют всё подряд по праву.
        "bot",
        "collector",
        "worker",
        "notifier",
        "dashboard",
        "runtime",
        # Клиент брокера отдельно: его изоляцию от базы сторожит
        # tests/test_broker_isolation.py, запуском в отдельном процессе.
        "broker",
        # Планировщик и источники: их границы проверяются тестами выше и
        # test_source_wiring.py.
        "search",
        "sources",
    }
    packages = {path.name for path in SRC.iterdir() if path.is_dir() and path.name != "__pycache__"}

    assert packages <= known, f"новые пакеты не описаны в тесте слоёв: {sorted(packages - known)}"
