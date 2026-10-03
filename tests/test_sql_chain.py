"""Цепочка `infra/sql/NNN_*.sql` не правит данные: деплой гонит её на КАЖДОМ запуске.

Таблицы применённых миграций нет, и `infra/deploy.sh` прогоняет каждый файл по
алфавиту при каждом деплое. Значит «разовая» правка данных в цепочке не
разовая: она исполнится снова после любого следующего деплоя и перепишет то, что
к тому времени решил кто-то другой. Так вышло с `008_housing_deal_type.sql`:
она переводила карточки жилья из продажи в аренду по регулярке и перебила бы
вердикты ИИ-проверки на 88 новых строках. А `007` и часть `003` давали упавшим
задачам лишние попытки в течение суток после любого деплоя.

Проверка статическая и без базы, поэтому идёт в каждом прогоне. Поведение на
живых данных проверяет `test_sql_chain_db.py`, но только при Postgres: правило
не должно зависеть от того, поднят ли он.
"""

from __future__ import annotations

import re
from pathlib import Path

SQL_DIR = Path(__file__).parents[1] / "infra" / "sql"
CHAIN = sorted(SQL_DIR.glob("[0-9][0-9][0-9]_*.sql"))

# Оператор, который меняет или удаляет УЖЕ лежащие данные. `INSERT ... ON
# CONFLICT DO NOTHING` сюда не входит: он только добавляет. А `DO UPDATE` и
# `MERGE` входят, потому что правят существующую строку так же, как UPDATE.
REWRITE = re.compile(
    r"^[ \t]*((?:UPDATE|DELETE[ \t]+FROM|TRUNCATE(?:[ \t]+TABLE)?|MERGE[ \t]+INTO)[ \t]+\S+)"
    r"|(ON[ \t]+CONFLICT[^;]*?DO[ \t]+UPDATE)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)

# Единственное разрешённое: `001_init.sql` заполняет пустые `created_at` и сразу
# после этого ставит NOT NULL. После первого прогона попадать в UPDATE нечему,
# так что это миграция колонки, а не правка данных.
ALLOWED = {("001_init.sql", "update notifications")}


def statements(path: Path) -> set[tuple[str, str]]:
    sql = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
    found = set()
    for match in REWRITE.finditer(sql):
        text = match.group(1) or match.group(2)
        found.add((path.name, " ".join(text.split()).lower()))
    return found


def test_the_chain_is_not_empty() -> None:
    """Пустой glob превратил бы остальные проверки в зелёные впустую."""
    assert len(CHAIN) >= 9, CHAIN


def test_the_chain_contains_no_data_rewrites() -> None:
    rewrites = set().union(*(statements(path) for path in CHAIN)) - ALLOWED

    assert not rewrites, (
        "в цепочке миграций появилась правка данных: деплой применяет её на "
        f"КАЖДОМ запуске, а не один раз. Найдено: {sorted(rewrites)}. Разовую "
        "правку делают вручную и описывают в docs/deploy.md, а не кладут сюда"
    )


def test_the_allow_list_still_matches_the_chain() -> None:
    """Устаревшее исключение молча разрешило бы вернуть то же самое позже."""
    present = set().union(*(statements(path) for path in CHAIN))

    assert ALLOWED <= present, f"исключение больше не нужно, уберите его: {ALLOWED - present}"


def test_the_seed_does_not_requeue_chats_that_are_already_joined_or_rejected() -> None:
    """Голый ON CONFLICT сверяет ключ только с очередью, а оттуда разобранных удаляют."""
    seed = re.sub(
        r"--[^\n]*", "", (SQL_DIR / "002_seed_candidates.sql").read_text(encoding="utf-8")
    )
    candidates = seed[
        seed.index("INSERT INTO chat_candidates") : seed.index("INSERT INTO chat_rejects")
    ]

    assert re.search(r"NOT EXISTS\s*\(\s*SELECT 1 FROM chats\b", candidates)
    assert re.search(r"NOT EXISTS\s*\(\s*SELECT 1 FROM chat_rejects\b", candidates)
