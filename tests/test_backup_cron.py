"""Расписание резервной копии: шаг деплоя ставит `/etc/cron.d/sniffer-backup` идемпотентно.

Копия, которой нет в расписании, не существует. Расписание — часть репозитория,
как и всё остальное на сервере (CLAUDE.md, «Никаких ручных правок»), поэтому его
ставит деплой, а не человек с `crontab -e`. Проверяется на подставном корне
файловой системы (`FS_ROOT`): настоящий `/etc` тесты не трогают ни при каких
правах запуска.

* файл ставится, не переписывается, если уже такой, и заменяется, если другой;
* cron недоступен, скрипта нет, путь недопустим, записать нельзя — шаг называет
  причину кодом возврата != 0 и ничего не оставляет за собой;
* сам шаг деплоя не обрывает деплой никогда: ни при `set -e`, ни при любой из
  этих причин, но предупреждает громко.
"""

from __future__ import annotations

import re

import pytest

from tests.deploy_support import script
from tests.shell_support import TRACE_CHMOD, Ran, function_source, needs_bash, run
from tests.sql_chain_support import ROOT

BACKUP = ROOT / "infra" / "backup" / "sniffer-pg-backup.sh"
LOG = "/var/log/sniffer-backup.log"


def _program(body: str, *, cron_dir: bool = True, has_script: bool = True) -> str:
    """Подставной корень: `$root/etc/cron.d` и `$root/deploy/infra/backup/<скрипт>`."""
    return "\n".join(
        [
            'info() { echo "INFO: $*"; }',
            function_source(script(), "backup_cron_content"),
            function_source(script(), "install_backup_cron"),
            'root="$(mktemp -d)"',
            """trap 'rm -rf "$root"' EXIT""",
            'export FS_ROOT="$root"',
            'DEPLOY_PATH="$root/deploy"',
            'mkdir -p "$DEPLOY_PATH/infra/backup"',
            ': > "$DEPLOY_PATH/infra/backup/sniffer-pg-backup.sh"' if has_script else "true",
            'mkdir -p "$root/etc/cron.d"' if cron_dir else "true",
            'CRON="$root/etc/cron.d/sniffer-backup"',
            body,
        ]
    )


def _run(body: str, **kwargs: bool) -> Ran:
    done = run(_program(body, **kwargs))
    assert done is not None
    return done


SHOW = (
    'echo "RC=$?"; echo "DP=$DEPLOY_PATH"; echo "---"; cat "$CRON"; echo "---"; stat -c %a "$CRON"'
)


# ── установка ────────────────────────────────────────────────────────────────


@needs_bash
def test_the_installer_writes_the_cron_file() -> None:
    done = _run("install_backup_cron; " + SHOW)

    head, content, mode = done.stdout.split("---\n")
    (deploy,) = re.findall(r"^DP=(.+)$", head, re.MULTILINE)
    lines = content.splitlines()
    assert "RC=0" in head and "cron резервной копии: установлен" in head
    assert lines[0].startswith("#") and "SHELL=/bin/bash" in lines
    assert lines[-1] == (
        f"0 3 * * * root bash {deploy}/infra/backup/sniffer-pg-backup.sh >>{LOG} 2>&1"
    )
    assert content.endswith("\n") and "\r" not in content
    assert mode.strip() == "644"


def test_cron_gets_a_full_path_before_the_job_line() -> None:
    """У cron PATH урезан до /usr/bin:/bin; `docker` и утилиты скрипта должны находиться."""
    lines = function_source(script(), "backup_cron_content").splitlines()
    paths = [line for line in lines if line.startswith("PATH=")]

    assert len(paths) == 1, "в cron-файле должна быть ровно одна строка PATH"
    dirs = paths[0].removeprefix("PATH=").split(":")
    assert {"/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"} <= set(dirs)
    job = next(i for i, line in enumerate(lines) if "sniffer-pg-backup.sh" in line)
    assert lines.index(paths[0]) < job, "PATH объявлен после задания и на него не действует"


def test_the_job_runs_at_03_00_server_time_utc() -> None:
    content = function_source(script(), "backup_cron_content")

    assert re.search(r"^0 3 \* \* \* root ", content, re.MULTILINE)


@needs_bash
def test_a_second_run_changes_nothing() -> None:
    body = "\n".join(
        [
            "install_backup_cron >/dev/null",
            """touch -d '2020-01-01 00:00:00' "$CRON"; before="$(stat -c %Y "$CRON")" """,
            'install_backup_cron; echo "RC=$?"',
            'after="$(stat -c %Y "$CRON")"',
            'echo "SAME=$([ "$before" = "$after" ] && echo yes || echo no)"',
        ]
    )

    done = _run(body)

    assert "RC=0" in done.stdout and "SAME=yes" in done.stdout, "файл переписан без нужды"
    assert "cron резервной копии: актуален" in done.stdout


@needs_bash
def test_a_different_file_is_replaced() -> None:
    done = _run("echo '# старая версия' > \"$CRON\"; install_backup_cron; " + SHOW)

    assert "RC=0" in done.stdout and "cron резервной копии: обновлён" in done.stdout
    assert "старая версия" not in done.stdout
    assert "sniffer-pg-backup.sh" in done.stdout


@needs_bash
def test_nothing_is_staged_after_an_install() -> None:
    done = _run('install_backup_cron >/dev/null; ls -A "$root/etc/cron.d"')

    assert done.stdout.split() == ["sniffer-backup"], "во временном файле остался мусор"


# ── отказы: громко, кодом возврата, и ничего за собой ────────────────────────


@needs_bash
def test_a_machine_without_cron_is_a_failure_that_creates_nothing() -> None:
    """Каталог `/etc/cron.d` создаёт пакет cron, а не деплой: нет каталога — нет cron."""
    done = _run('install_backup_cron; echo "RC=$?"; ls -A "$root"', cron_dir=False)

    assert "RC=1" in done.stdout
    assert "cron недоступен" in done.stderr
    assert done.stdout.split()[-1] == "deploy", "шаг создал каталоги там, где cron не стоит"


@needs_bash
def test_a_missing_script_is_a_failure_and_no_cron_file() -> None:
    """Расписание на несуществующий скрипт тихо падало бы каждую ночь."""
    done = _run('install_backup_cron; echo "RC=$?"; ls -A "$root/etc/cron.d"', has_script=False)

    assert "RC=1" in done.stdout
    assert "нет скрипта резервной копии" in done.stderr
    assert "sniffer-backup" not in done.stdout


@needs_bash
@pytest.mark.parametrize("name", ["with space", "semi;colon", "amp&ersand"])
def test_an_unsafe_deploy_path_is_refused(name: str) -> None:
    """Путь попадает в строку cron как есть: пробел или `;` изменили бы саму команду."""
    body = "\n".join(
        [
            f'DEPLOY_PATH="$root/{name}"',
            'mkdir -p "$DEPLOY_PATH/infra/backup"',
            ': > "$DEPLOY_PATH/infra/backup/sniffer-pg-backup.sh"',
            'install_backup_cron; echo "RC=$?"; ls -A "$root/etc/cron.d"',
        ]
    )

    done = _run(body)

    assert "RC=1" in done.stdout
    assert "недопустимые" in done.stderr
    assert "sniffer-backup" not in done.stdout


@needs_bash
def test_a_relative_deploy_path_is_refused() -> None:
    """У cron своя рабочая папка: относительный путь в строке задания указывал бы в никуда."""
    body = 'cd "$root"; DEPLOY_PATH="deploy"; '
    body += 'install_backup_cron; echo "RC=$?"; ls -A "$root/etc/cron.d"'

    done = _run(body)

    assert "RC=1" in done.stdout
    assert "абсолютным" in done.stderr
    assert "sniffer-backup" not in done.stdout


@needs_bash
def test_a_target_that_cannot_be_written_is_a_failure_without_leftovers() -> None:
    body = 'mkdir "$CRON"; install_backup_cron; echo "RC=$?"; ls -A "$root/etc/cron.d"'

    done = _run(body)

    assert "RC=1" in done.stdout
    assert "не удалось записать" in done.stderr
    assert done.stdout.split()[-1] == "sniffer-backup", "остался временный файл"


@needs_bash
def test_a_write_that_silently_did_nothing_is_caught() -> None:
    """`mv` вернул 0, а файла нет: «установлен» без файла — худшая из ошибок."""
    done = _run('mv() { return 0; }; install_backup_cron; echo "RC=$?"')

    assert "RC=1" in done.stdout
    assert "не совпал" in done.stderr
    assert "установлен" not in done.stdout


@needs_bash
def test_the_cron_file_is_asked_to_be_readable_whatever_the_umask() -> None:
    """Под строгой umask `cat >` создал бы файл 0600: права задаются явно, а не наследуются."""
    body = (
        "\n".join(
            [
                'export TRACE="$root/trace.txt"',
                TRACE_CHMOD,
                "umask 077",
                "install_backup_cron >/dev/null",
            ]
        )
        + '; cat "$TRACE"'
    )

    done = _run(body)

    assert re.search(r"^0644 \S+/etc/cron\.d/sniffer-backup\.new$", done.stdout, re.MULTILINE), (
        done.text
    )


# ── шаг деплоя: предупреждает, но не обрывает ────────────────────────────────


def _step() -> str:
    found = re.search(r"^# ── 4\.75 .*?(?=^# ── 5\. )", script(), re.MULTILINE | re.DOTALL)
    assert found, "шага «резервная копия БД» (4.75) в deploy.sh нет"
    return found.group(0)


def _step_program(installer: str) -> str:
    return "\n".join(
        [
            "set -euo pipefail",
            'log() { echo "LOG: $*"; }',
            installer,
            _step(),
            'echo "AFTER BACKUP_WARN=$BACKUP_WARN"',
        ]
    )


@needs_bash
def test_a_failed_install_warns_loudly_and_the_deploy_goes_on() -> None:
    """Под `set -e` голый вызов оборвал бы деплой кодом 1 — его нет в таблице кодов."""
    done = run(_step_program("install_backup_cron() { return 1; }"))

    assert done is not None and done.code == 0, done and done.text
    assert "AFTER BACKUP_WARN=1" in done.stdout
    assert "ВНИМАНИЕ" in done.stderr
    assert "::warning::" in done.stdout


@needs_bash
def test_a_good_install_is_silent() -> None:
    done = run(_step_program("install_backup_cron() { return 0; }"))

    assert done is not None and done.code == 0, done and done.text
    assert "AFTER BACKUP_WARN=0" in done.stdout
    assert "ВНИМАНИЕ" not in done.text and "::warning::" not in done.text


@needs_bash
def test_the_real_installer_without_cron_does_not_abort_the_step() -> None:
    """Те же условия, что на машине без cron: настоящая функция под `set -e`."""
    program = "\n".join(
        [
            function_source(script(), "backup_cron_content"),
            function_source(script(), "install_backup_cron"),
            'info() { echo "INFO: $*"; }',
            'export FS_ROOT="$(mktemp -d)"',
            """trap 'rm -rf "$FS_ROOT"' EXIT""",
            'DEPLOY_PATH="$FS_ROOT/deploy"; mkdir -p "$DEPLOY_PATH/infra/backup"',
            ': > "$DEPLOY_PATH/infra/backup/sniffer-pg-backup.sh"',
            _step_program(""),
        ]
    )

    done = run(program)

    assert done is not None and done.code == 0, done and done.text
    assert "AFTER BACKUP_WARN=1" in done.stdout


def test_the_end_of_the_deploy_repeats_the_warning() -> None:
    """Предупреждение посреди журнала тонет: последней строкой оно должно быть ещё раз."""
    text = script()
    reminder = text.index('if [ "${BACKUP_WARN:-0}" -ne 0 ]; then')

    assert text.index('log "проверка пройдена: деплой успешен"') < reminder
    assert reminder < text.index('log "готово: $NEW_SHA"')


def test_the_scheduled_script_is_the_one_in_the_repository() -> None:
    assert BACKUP.is_file()
    assert "infra/backup/sniffer-pg-backup.sh" in function_source(script(), "backup_cron_content")
