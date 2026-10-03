"""Колонки-часовые деплоя: правило «свежий ALTER — свой часовой» держит CI, а не память.

Часовой — строка `require_column <таблица> <колонка>` в `infra/deploy.sh`, которая
валит деплой, если ALTER не доехал до живой базы (живой отказ 02.09.2026: таблицы
были, колонок не было, а деплой рапортовал успех). Охрана у такой проверки
хрупкая в трёх местах, и каждое было найдено пробной мутацией, а не чтением:

* часовой заменили константой (`HAS_NEW=1`) — проверка исчезла, деплой зелёный;
* в имени колонки опечатка, записанная в другом регистре, — деплой вечно красный;
* новая колонка появилась, а часового на ней нет — и никто этого не заметил.

Поэтому здесь проверяется не вид строк, а три вещи. Пары «таблица — колонка»
читаются из самого скрипта и сверяются с моделями и с ALTER ИМЕННО ЭТОЙ таблицы.
Любой ALTER, которого не было на момент введения правила, обязан иметь часового
(храповик: список старых закрыт и только сокращается). А сама функция
исполняется на подставном `docker`, и по её поведению видно, что отсутствие
колонки красит деплой, а присутствие — нет.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from sniffer.db import collection_models as _collection_models  # noqa: F401
from sniffer.db.models import Base

ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = ROOT / "infra" / "sql"
DEPLOY = ROOT / "infra" / "deploy.sh"

# Вызов часового: отдельная строка, хвостовой комментарий допускается.
_CALL = re.compile(r"^[ \t]*require_column[ \t]+(\w+)[ \t]+(\w+)[ \t]*(?:#.*)?$", re.MULTILINE)
_FUNCTION = re.compile(r"^require_column\(\) \{\n.*?^\}\n", re.MULTILINE | re.DOTALL)
_ALTER = re.compile(r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(\w+)\s+(.*?)(?:;|\Z)", re.I | re.S)
_ADD = re.compile(r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)", re.I)

# ALTER'ы, существовавшие к введению правила, у которых часового нет. Список
# ЗАКРЫТ: новая строка в него — это обход правила, и ревью обязано её заметить.
# Список только сокращается: появился часовой — строка отсюда уходит, иначе
# `test_the_legacy_list_only_names_real_unguarded_alters` краснеет.
LEGACY_WITHOUT_SENTINEL = frozenset(
    {
        "chats.backfill_msg_id",
        "chats.backfill_done",
        "listings.external_id",
        "listings.catalog_observation_id",
        "listings.screened_at",
        "listings.screen_note",
        "users.active_passport_root",
        "users.editing_passport_root",
        "subscriptions.since_listing_id",
        "subscriptions.scan_listing_id",
        "subscriptions.expires_at",
        "subscriptions.charge_id",
        "notifications.created_at",
        "outbox.subscription_id",
        "outbox.notification_id",
        "collection_subscribers.reply_queued_at",
    }
)


def _script() -> str:
    return DEPLOY.read_text(encoding="utf-8")


def _sentinels() -> list[tuple[str, str]]:
    return _CALL.findall(_script())


def _alters() -> set[tuple[str, str]]:
    """Пары «таблица — колонка» из всех `ALTER TABLE … ADD COLUMN IF NOT EXISTS`."""
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(SQL_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    )
    text = re.sub(r"--[^\n]*", "", text)
    return {
        (match.group(1), column)
        for match in _ALTER.finditer(text)
        for column in _ADD.findall(match.group(2))
    }


def _guard() -> str:
    found = _FUNCTION.search(_script())
    assert found, "функции require_column в deploy.sh нет — часовые стали вызовами в пустоту"
    return found.group(0)


# ── что часовой называет ────────────────────────────────────────────────────


def test_the_script_has_sentinels_at_all() -> None:
    assert _sentinels(), "часовых не нашлось — проверка охраняет пустоту"


def test_there_is_exactly_one_column_probe_and_it_is_the_function() -> None:
    """Вторая копия запроса в скрипте — это проверка, которую можно ослабить отдельно.

    Именно так жила константа `HAS_NEW=1`: блок был копией, и подменить одну копию
    не стоило ничего. Запрос к `information_schema.columns` один — в функции.
    """
    probe = "information_schema.columns"

    assert _script().count(probe) == _guard().count(probe) == 1, (
        "запрос к information_schema.columns вне require_column: часовой-копия"
    )


def test_every_sentinel_names_a_model_column_that_an_alter_adds_to_that_table() -> None:
    """Опечатка делает часового вечно красным, переименование — вечно зелёным.

    ALTER сверяется ПО ТАБЛИЦЕ: `users.created_at` прошёл бы сверку «где-нибудь
    есть ADD COLUMN created_at» за счёт `notifications`, хотя на `users` такого
    ALTER нет и часовой охранял бы чужую колонку.
    """
    alters = _alters()

    for table, column in _sentinels():
        assert table in Base.metadata.tables, f"{table}: нет такой таблицы в моделях"
        assert column in {c.name for c in Base.metadata.tables[table].columns}, (
            f"{table}.{column}: нет в моделях"
        )
        assert (table, column) in alters, (
            f"{table}.{column}: ни одного `ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
            f"{column}` — на свежей базе часовой зелёный, на живой красный"
        )


def test_a_sentinel_is_not_listed_twice() -> None:
    pairs = _sentinels()

    assert len(pairs) == len(set(pairs)), "один и тот же часовой дважды"


# ── храповик: свежий ALTER без часового краснит сборку ──────────────────────


def test_a_fresh_alter_without_a_sentinel_turns_the_build_red() -> None:
    """Правило «новая колонка — свой часовой» исполняется, а не помнится.

    «Свежий» здесь — любой ALTER, которого нет в закрытом списке старых. Забыл
    часового — упал этот тест, и сообщение называет, что дописать. Убрали
    существующий часовой — упал он же: колонка снова «свежая и без охраны».
    """
    guarded = set(_sentinels())
    fresh = {f"{table}.{column}" for table, column in _alters() - guarded}
    unguarded = sorted(fresh - LEGACY_WITHOUT_SENTINEL)

    assert not unguarded, (
        "ALTER без часового: "
        + ", ".join(unguarded)
        + " — допишите в infra/deploy.sh строку `require_column <таблица> <колонка>`"
    )


def test_the_legacy_list_only_names_real_unguarded_alters() -> None:
    """Закрытый список не врёт: в нём нет ни выдуманных, ни уже охраняемых строк."""
    alters = {f"{table}.{column}" for table, column in _alters()}
    guarded = {f"{table}.{column}" for table, column in _sentinels()}

    assert LEGACY_WITHOUT_SENTINEL <= alters, sorted(LEGACY_WITHOUT_SENTINEL - alters)
    assert not LEGACY_WITHOUT_SENTINEL & guarded, "часовой появился — уберите строку из списка"


# ── сама функция: красит ли она деплой ──────────────────────────────────────


def _bash(program: str) -> str | None:
    """Выполнить программу в bash и вернуть stdout+stderr. `None` — bash недоступен.

    Программа уходит на stdin БАЙТАМИ: в текстовом режиме Windows превратил бы
    `\\n` в `\\r\\n`, и bash читал бы чужой скрипт с `\\r` в конце каждой строки.
    """
    exe = shutil.which("bash")
    if exe is None:
        return None
    try:
        done = subprocess.run(  # noqa: S603 - фиксированный argv
            [exe, "-s"],
            input=program.encode("utf-8"),
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (done.stdout + done.stderr).decode("utf-8", errors="replace")
    return f"{text}\nEXIT={done.returncode}"


def _bash_works() -> bool:
    out = _bash("echo ok")
    return out is not None and out.startswith("ok")


needs_bash = pytest.mark.skipif(not _bash_works(), reason="bash недоступен: функцию не запустить")


def _run_guard(
    answer: str,
    table: str = "users",
    column: str = "awaiting_new_request",
    *,
    docker_exit: int = 0,
) -> str:
    """Исполняет `require_column` против подставного `docker` и отдаёт итог одним текстом.

    Подставной `docker` отвечает `answer` ТОЛЬКО на запрос про
    `users.awaiting_new_request` (по тексту запроса, как настоящая база), на всё
    остальное — нулём: перепутанные местами таблица и колонка или чужая колонка
    в запросе видны как «часовой не нашёл колонку». Порядок условий в запросе
    не важен. `docker_exit` — код возврата самого `docker`: контейнер мог не
    ответить вовсе.
    """
    program = "\n".join(
        [
            "FAIL=0",
            "PG_CID=fake",
            'info() { echo "INFO: $*"; }',
            "docker() {",
            f"  if [ {docker_exit} -ne 0 ]; then return {docker_exit}; fi",
            '  if [[ "$*" == *"table_name=\'users\'"* && '
            '"$*" == *"column_name=\'awaiting_new_request\'"* ]]; then',
            f"    printf '%s\\n' '{answer}'",
            "  else",
            "    printf '0\\n'",
            "  fi",
            "}",
            _guard(),
            f"require_column {table} {column}",
            'echo "FAIL=$FAIL"',
        ]
    )
    out = _bash(program)
    assert out is not None
    return out


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
def test_the_deploy_script_parses() -> None:
    """Опечатка в скрипте красит каждый деплой, а ловится только на сервере."""
    exe = shutil.which("bash")
    assert exe is not None
    done = subprocess.run(  # noqa: S603 - фиксированный argv
        [exe, "-n"],
        input=_script().encode("utf-8"),
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr.decode("utf-8", errors="replace")
