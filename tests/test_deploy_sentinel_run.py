"""Часовые исполняются: функция и цикл — против подставного `docker`, как в деплое.

Читать скрипт мало: константа `HAS_NEW=1` на месте проверки и опечатка в другом
регистре пережили прежний структурный тест. Поэтому здесь `require_column` и цикл
по таблице `schema_sentinels` запускаются настоящим bash, а по их поведению видно:
отсутствие колонки или таблицы, упавший `docker` и ответ, в котором не число,
красят деплой одинаково, а присутствие — нет.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from tests.deploy_support import Row, script, sentinel_rows
from tests.shell_support import function_source, needs_bash, run

QUOTE = "'"
FUNCTIONS = ("require_column", "schema_sentinels", "check_schema_sentinels")


def _docker(target: Row | None, answer: str, other: str, docker_exit: int = 0) -> str:
    """Подставной `docker`: `answer` на запрос про `target`, `other` на любой другой.

    Запрос узнаётся по тексту, как у настоящей базы: таблица и колонка в нём
    названы. Порядок условий не важен. У строки без колонки в запросе не должно
    быть и фильтра по колонке. `docker_exit` — код возврата самого `docker`:
    контейнер мог не ответить вовсе.
    """
    lines = ["docker() {", f"  if [ {docker_exit} -ne 0 ]; then return {docker_exit}; fi"]
    if target is not None:
        table, column = target
        asked = f'"$*" == *"table_name={QUOTE}{table}{QUOTE}"*'
        if column is None:
            asked += ' && "$*" != *"column_name"*'
        else:
            asked += f' && "$*" == *"column_name={QUOTE}{column}{QUOTE}"*'
        lines += [f"  if [[ {asked} ]]; then echo {QUOTE}{answer}{QUOTE}; return 0; fi"]
    lines += [f"  echo {QUOTE}{other}{QUOTE}", "}"]
    return "\n".join(lines)


def _program(body: str, docker: str) -> str:
    return "\n".join(
        [
            "FAIL=0",
            "PG_CID=fake",
            'info() { echo "INFO: $*"; }',
            docker,
            *[function_source(script(), name) for name in FUNCTIONS],
            body,
            'echo "FAIL=$FAIL"',
        ]
    )


def _run(body: str, docker: str) -> str:
    done = run(_program(body, docker))
    assert done is not None
    return done.text


def _name(row: Row) -> str:
    table, column = row
    return f"{table}.{column}" if column else f"таблица {table}"


def _run_guard(
    answer: str,
    table: str = "users",
    column: str | None = "awaiting_new_request",
    *,
    target: Row = ("users", "awaiting_new_request"),
    docker_exit: int = 0,
) -> str:
    """`require_column` против `docker`, который отвечает `answer` только про `target`."""
    call = f"require_column {table} {column or ''}"
    return _run(call, _docker(target, answer, "0", docker_exit))


# ── функция: красит ли она деплой ───────────────────────────────────────────


@needs_bash
def test_a_present_column_leaves_the_deploy_green() -> None:
    out = _run_guard("1")

    assert "FAIL=0" in out
    assert "users.awaiting_new_request на месте" in out


@needs_bash
@pytest.mark.parametrize(
    "answer", ["0", "", "ERROR", "t"], ids=["zero", "empty", "error", "garbage"]
)
def test_a_missing_column_or_an_unreadable_answer_turns_the_deploy_red(answer: str) -> None:
    """Не число — то же, что ноль: `[ x -lt 1 ]` на не-числе молча уходит в «на месте»."""
    out = _run_guard(answer)

    assert "FAIL=1" in out
    assert "users.awaiting_new_request отсутствует" in out


@needs_bash
def test_a_docker_that_does_not_answer_turns_the_deploy_red() -> None:
    """Контейнер базы мог лежать: молчание — не «колонка на месте»."""
    out = _run_guard("", docker_exit=1)

    assert "FAIL=1" in out
    assert "users.awaiting_new_request отсутствует" in out


@needs_bash
def test_the_probe_asks_about_the_named_table_and_column() -> None:
    """Подставной docker отвечает «1» только на `users.awaiting_new_request`.

    Вызов про другую колонку обязан получить «нет»: иначе функция спрашивает не
    о том, что ей назвали.
    """
    out = _run_guard("1", table="users", column="is_blocked")

    assert "FAIL=1" in out
    assert "users.is_blocked отсутствует" in out


@needs_bash
def test_a_row_without_a_column_checks_the_whole_table() -> None:
    """Однословная строка — таблица целиком: запрос без фильтра по колонке."""
    target = ("probe_table", None)

    present = _run_guard("1", "probe_table", None, target=target)
    missing = _run_guard("0", "probe_table", None, target=target)

    assert "FAIL=0" in present and "таблица probe_table на месте" in present
    assert "FAIL=1" in missing and "таблица probe_table отсутствует" in missing


# ── цикл: читает ли он таблицу целиком ──────────────────────────────────────


@needs_bash
def test_every_row_present_leaves_the_deploy_green() -> None:
    out = _run("check_schema_sentinels", _docker(None, "", "1"))

    assert "FAIL=0" in out
    assert out.count("на месте") == len(sentinel_rows())


@needs_bash
@pytest.mark.parametrize("row", sentinel_rows(), ids=_name)
def test_a_missing_row_turns_the_deploy_red_and_the_rest_are_still_probed(row: Row) -> None:
    """Каждая строка проверяется под своим именем, и красная не обрывает цикл."""
    out = _run("check_schema_sentinels", _docker(row, "0", "1"))

    assert "FAIL=1" in out
    assert f"{_name(row)} отсутствует" in out
    assert out.count("отсутствует") == 1
    assert out.count("на месте") == len(sentinel_rows()) - 1


@needs_bash
def test_a_docker_that_does_not_answer_turns_every_row_red() -> None:
    out = _run("check_schema_sentinels", _docker(None, "", "1", docker_exit=1))

    assert "FAIL=1" in out
    assert out.count("отсутствует") == len(sentinel_rows())
    assert "на месте" not in out


@needs_bash
def test_comments_and_blank_lines_in_the_table_are_not_probed() -> None:
    """Хвостовой комментарий однословной строки не должен стать её колонкой."""
    table = "\n".join(
        ["# заголовок", "", "alpha  one   # хвост", "beta         # без колонки", "  ", "#gamma  x"]
    )
    body = "\n".join(
        ["schema_sentinels() {", "  cat <<'ROWS'", table, "ROWS", "}", "check_schema_sentinels"]
    )

    out = _run(body, _docker(None, "", "1"))

    assert "FAIL=0" in out
    assert out.count("на месте") == 2
    assert "alpha.one на месте" in out and "таблица beta на месте" in out
    assert "gamma" not in out


@needs_bash
def test_the_shell_reads_the_same_rows_the_tests_do() -> None:
    """Разбор строк в Python (им сверяются модели и ALTER) и в bash (им ходит деплой) един."""
    body = "\n".join(['require_column() { echo "ROW|$1|${2:-}"; }', "check_schema_sentinels"])

    out = _run(body, _docker(None, "", "1"))

    seen = [tuple(line.split("|")[1:]) for line in out.splitlines() if line.startswith("ROW|")]
    assert seen == [(table, column or "") for table, column in sentinel_rows()]


@needs_bash
def test_the_deploy_script_parses() -> None:
    """Опечатка в скрипте красит каждый деплой, а ловится только на сервере."""
    exe = shutil.which("bash")
    assert exe is not None
    done = subprocess.run(  # noqa: S603 - фиксированный argv
        [exe, "-n"],
        input=script().encode("utf-8"),
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr.decode("utf-8", errors="replace")
