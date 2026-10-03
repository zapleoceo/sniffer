"""Маска миграций видит трёхзначные номера: файл `010_*.sql` не должен молча не примениться.

Деплой и CI берут файлы цепочки по маске. Прежняя маска `00*.sql` к `010_...` не
подходит: таблицы денег и прав лежали бы в репозитории и не доехали ни до боевой
базы, ни до тестовой, и ни одной ошибки при этом бы не было — зелёный деплой над
отсутствующей таблицей. Маска вынимается из самих скриптов, а не переписывается
здесь второй копией: копия проверяла бы саму себя.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOOP = re.compile(r"for migration in infra/sql/(\S+\.sql); do")
CONSUMERS = ["infra/deploy.sh", ".github/workflows/quality.yml"]


def mask(consumer: str) -> str:
    found = LOOP.search((ROOT / consumer).read_text(encoding="utf-8"))
    assert found, f"{consumer}: цикла по миграциям не нашлось — проверять нечего"
    return found.group(1)


@pytest.mark.parametrize("consumer", CONSUMERS)
@pytest.mark.parametrize(
    "name", ["001_init.sql", "009_listing_screen.sql", "010_quota_ledger.sql", "123_probe.sql"]
)
def test_the_mask_picks_up_every_numbered_file(consumer: str, name: str) -> None:
    assert fnmatch.fnmatchcase(name, mask(consumer)), f"{consumer}: {name} не применится"


@pytest.mark.parametrize("consumer", CONSUMERS)
@pytest.mark.parametrize(
    "name", ["readme.sql", "10_short.sql", "0100_four.sql", "010_probe.txt", "x010_probe.sql"]
)
def test_the_mask_ignores_what_is_not_a_chain_file(consumer: str, name: str) -> None:
    assert not fnmatch.fnmatchcase(name, mask(consumer)), f"{consumer}: {name} попал в цепочку"
