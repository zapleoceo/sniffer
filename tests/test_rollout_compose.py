"""Public catalogue rollout is an explicit, reviewable compose contract."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

COMPOSE = Path(__file__).parents[1] / "docker-compose.yml"
DEPLOY = Path(__file__).parents[1] / "infra" / "deploy.sh"
WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "deploy.yml"


def _service_block(name: str) -> str:
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"  {name}:")
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("  ")
            and not lines[index].startswith("    ")
            and lines[index].strip()
            and not lines[index].lstrip().startswith("#")
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_public_bot_answers_from_the_listings_catalogue() -> None:
    """Владелец 12.09.2026: ответ — из своей базы, без живого обхода групп."""
    bot = _service_block("bot")

    assert "CATALOG_MODE: listings" in bot
    assert 'AGENT_COLLECTOR_ENABLED: "true"' in bot


def test_hourly_collector_is_enabled_in_its_profile() -> None:
    collector = _service_block("agent-collector")

    assert 'profiles: ["agent-catalog"]' in collector
    assert 'AGENT_COLLECTOR_ENABLED: "true"' in collector


def test_deploy_includes_profile_and_rejects_idle_public_collector() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")

    assert 'COMPOSE_PROFILE_ARGS="--profile agent-catalog"' in deploy
    assert "docker compose $COMPOSE_PROFILE_ARGS up -d --remove-orphans" in deploy
    assert 's.catalog_mode in ("catalog", "listings")' in deploy
    assert "s.agent_collector_enabled" in deploy
    assert "s.broker_project_key.strip()" in deploy


# ── вывод деплоя уходит в журнал ПУБЛИЧНОГО репозитория ──────────────────────
#
# Деплой печатал `docker compose logs --tail 50` на каждом запуске: 76 прогонов
# выложили id чатов, названия групп и параметры SQL. Читать журнал может любой
# залогиненный пользователь GitHub. Секретов там не нашли, но канал был открыт, и
# любая ошибка с параметрами уходила наружу автоматически.

FAKE_DOCKER = r"""
docker() {
  if [ "$*" = "compose ps --services" ]; then printf 'bot\nworker\n'; return 0; fi
  case "$*" in
    "compose logs"*) cat <<'LOG'
{"event": "history_synced", "level": "info", "chat": "-100SECRETCHAT", "title": "SECRETTITLE"}
{"event": "history_synced", "level": "info", "chat": "-100SECRETCHAT2"}
{"event": "join.failed", "level": "error", "detail": "SECRETDETAIL"}
{"event": "SECRETFREETEXT joined the chat", "level": "info"}
[parameters: ('SECRETSQLPARAM',)]
Traceback (most recent call last):
ERROR: relation "x" does not exist SECRETPG
LOG
    ;;
    *) echo "неожиданный вызов docker: $*" >&2; return 1 ;;
  esac
}
export -f docker
"""


def test_deploy_workflow_never_prints_container_logs() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "compose logs" not in workflow
    assert "docker logs" not in workflow
    assert "infra/deploy.sh --summary" in workflow


def test_deploy_script_reads_container_logs_only_to_count_them() -> None:
    """Единственный `docker compose logs` стоит в сводке, и его вывод уходит в переменную."""
    script = DEPLOY.read_text(encoding="utf-8")
    start = script.index("log_summary() {")
    end = script.index("\n}\n", start)
    calls = list(re.finditer(r"^(?!\s*info ).*(?:docker compose|docker) logs.*$", script, re.M))

    assert calls, "проверка не смотрит на то, что должна: вызова logs нет вовсе"
    for call in calls:
        assert start < call.start() < end, f"сырые логи вне сводки: {call.group(0).strip()}"
        assert re.match(r'\s*\w+="\$\(docker compose logs', call.group(0)), (
            f"вывод логов не уходит в переменную: {call.group(0).strip()}"
        )


@pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")
def test_the_log_summary_prints_counts_and_never_a_log_line(tmp_path: Path) -> None:
    """Подставной docker с «секретами» в логах: наружу выходят только числа и идентификаторы."""
    fake = tmp_path / "fake_docker.sh"
    fake.write_text(FAKE_DOCKER, encoding="utf-8", newline="\n")
    command = (
        f'source "{fake.as_posix()}" && '
        f'exec bash "{DEPLOY.as_posix()}" --summary "{tmp_path.as_posix()}"'
    )

    bash = shutil.which("bash")
    assert bash is not None
    done = subprocess.run(  # noqa: S603
        [bash, "-c", command], capture_output=True, text=True, encoding="utf-8", timeout=60
    )

    assert done.returncode == 0, done.stderr
    assert "SECRET" not in done.stdout + done.stderr, "в вывод попала строка лога"
    assert "Traceback" not in done.stdout
    assert "relation" not in done.stdout
    assert "history_synced=2" in done.stdout
    assert "join.failed=1" in done.stdout
    assert "ошибок 3" in done.stdout
    assert "joined" not in done.stdout, "название события, не похожее на идентификатор"
