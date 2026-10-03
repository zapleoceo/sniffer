"""Документация резервной копии не расходится со скриптом: числа берутся из кода.

«Разъехавшаяся документация хуже отсутствующей: ей верят» (CLAUDE.md). В разделе
10 `docs/deploy.md` записаны каталог, срок хранения, порог размера, расписание и
коды выхода, и каждое из них живёт в коде. Здесь они сверяются с текстом раздела:
поменяли значение по умолчанию — краснеет, пока не поправлена и инструкция.
"""

from __future__ import annotations

import re

from tests.deploy_support import script
from tests.shell_support import function_source
from tests.sql_chain_support import ROOT

BACKUP = (ROOT / "infra" / "backup" / "sniffer-pg-backup.sh").read_text(encoding="utf-8")
DOCS = (ROOT / "docs" / "deploy.md").read_text(encoding="utf-8")


def _section() -> str:
    start = DOCS.index("## 10. Резервная копия БД sniffer\n")
    return DOCS[start : DOCS.index("## 11. ", start)]


def _default(name: str) -> str:
    found = re.search(rf'^{name}="\$\{{{name}:-([^}}]+)\}}"$', BACKUP, re.MULTILINE)
    assert found, f"у скрипта нет значения по умолчанию для {name}"
    return found.group(1)


def test_the_section_states_the_defaults_of_the_script() -> None:
    section = _section()

    assert _default("BACKUP_DIR") in section
    assert f"**{_default('KEEP_DAYS')} суток**" in section
    assert f"{_default('MIN_BYTES')} байт" in section


def test_the_section_states_the_schedule_and_the_log_of_the_cron_file() -> None:
    content = function_source(script(), "backup_cron_content")
    job = re.search(r"^(\d+) (\d+) \* \* \* root .*>>(\S+) 2>&1$", content, re.MULTILINE)
    assert job, "в cron-файле нет строки задания"
    minute, hour, log = job.groups()

    assert f"{int(hour):02d}:{int(minute):02d}" in _section()
    assert log in _section()


def test_every_exit_code_of_the_script_is_in_the_table_of_the_section() -> None:
    codes = set(re.findall(r'die "[^"]*" (\d)', BACKUP))
    documented = set(re.findall(r"^\| (\d) \|", _section(), re.MULTILINE))

    assert codes == {"1", "2", "3", "4"}, f"коды скрипта изменились: {sorted(codes)}"
    assert documented == codes, "таблица кодов выхода в docs/deploy.md расходится со скриптом"
