"""Цепочка миграций глазами тестов: маска, каталог и разбор DDL — в одном месте.

Маску файлов тесты вычисляли сами — пять копий двузначной маски, — и все пять
молчали о файле `010_*.sql` ровно так же, как деплой и CI. Знание «какие файлы
входят в цепочку» должно быть одно. Деплой и CI — не Python, их маска записана
в `infra/deploy.sh` и `.github/workflows/quality.yml`, и сверяет их с этой
константой `tests/test_migration_mask.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = ROOT / "infra" / "sql"

# Файл цепочки: трёхзначный номер, подчёркивание, имя. Порядок применения —
# алфавитный; файлов с одним номером может быть несколько (`002_*`, `003_*`).
MASK = "[0-9][0-9][0-9]_*.sql"

_ALTER = re.compile(r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(\w+)\s+(.*?)(?:;|\Z)", re.I | re.S)
_ADD = re.compile(r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)", re.I)


def chain(directory: Path = SQL_DIR) -> list[Path]:
    """Файлы цепочки по алфавиту — в том порядке, в каком их применяет деплой."""
    return sorted(directory.glob(MASK))


def ddl(directory: Path = SQL_DIR) -> str:
    """Цепочка одним текстом, без комментариев."""
    text = "\n".join(path.read_text(encoding="utf-8") for path in chain(directory))
    return re.sub(r"--[^\n]*", "", text)


def added_columns(directory: Path = SQL_DIR) -> set[tuple[str, str]]:
    """Пары «таблица — колонка» из всех `ALTER TABLE … ADD COLUMN IF NOT EXISTS`."""
    return {
        (match.group(1), column)
        for match in _ALTER.finditer(ddl(directory))
        for column in _ADD.findall(match.group(2))
    }
