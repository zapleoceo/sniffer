"""`infra/deploy.sh` глазами тестов: текст скрипта и таблица часовых схемы."""

from __future__ import annotations

import re

from tests.shell_support import function_source
from tests.sql_chain_support import ROOT

DEPLOY = ROOT / "infra" / "deploy.sh"

# Строка таблицы часовых: (таблица, колонка). Колонки нет — охраняется таблица целиком.
Row = tuple[str, str | None]

_WORD = re.compile(r"\w+")
_OPEN = "<<'SENTINELS'\n"
_CLOSE = "\nSENTINELS\n"


def script() -> str:
    return DEPLOY.read_text(encoding="utf-8")


def parse_rows(table: str) -> list[Row]:
    """Строки `таблица колонка` или `таблица`; хвостовой комментарий и пустые строки допустимы."""
    rows: list[Row] = []
    for line in table.splitlines():
        words = line.split("#", 1)[0].split()
        if not words:
            continue
        assert 1 <= len(words) <= 2 and all(_WORD.fullmatch(word) for word in words), (
            f"строка часового не по форме «таблица [колонка]»: {line!r}"
        )
        rows.append((words[0], words[1] if len(words) == 2 else None))
    return rows


def sentinel_rows() -> list[Row]:
    """Таблица часовых из функции `schema_sentinels` в deploy.sh."""
    body = function_source(script(), "schema_sentinels")
    start = body.index(_OPEN) + len(_OPEN)
    return parse_rows(body[start : body.index(_CLOSE)])
