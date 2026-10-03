"""Каждый потребитель цепочки миграций видит файл `011_*`, а не только `001`–`009`.

Деплой, CI и тесты схемы берут файлы по маске. Маска `00*.sql` молча пропускает
`010_*.sql` и дальше: схема новой таблицы не доезжает ни до сервера, ни до тестовой
базы, а деплой рапортует успех (тот же отказ 02.09.2026, что и у колонок без часового).
Проверяется сама маска, вынутая из скрипта и из workflow, а не её копия в тесте.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APPLIERS = (ROOT / "infra" / "deploy.sh", ROOT / ".github" / "workflows" / "quality.yml")
LOOP = re.compile(r"for migration in infra/sql/(\S+?); do")


def _mask(path: Path) -> str:
    found: list[str] = LOOP.findall(path.read_text(encoding="utf-8"))
    assert len(found) == 1, f"{path.name}: цикл применения миграций не найден или не один"
    return found[0]


@pytest.mark.parametrize("applier", APPLIERS, ids=lambda path: path.name)
@pytest.mark.parametrize(
    "name", ["001_init.sql", "009_x.sql", "010_x.sql", "011_stars_billing.sql"]
)
def test_the_mask_takes_every_numbered_file(applier: Path, name: str) -> None:
    assert fnmatch.fnmatchcase(name, _mask(applier)), f"{applier.name} не применит {name}"


@pytest.mark.parametrize("applier", APPLIERS, ids=lambda path: path.name)
@pytest.mark.parametrize("name", ["readme.sql", "0011_x.sql", "11_x.sql", "011_x.txt"])
def test_the_mask_does_not_take_what_is_not_a_migration(applier: Path, name: str) -> None:
    assert not fnmatch.fnmatchcase(name, _mask(applier)), f"{applier.name} применит {name}"


def test_both_appliers_use_the_same_mask() -> None:
    """Разные маски у сервера и у CI означали бы схему, которую один видит, а другой нет."""
    assert len({_mask(path) for path in APPLIERS}) == 1
