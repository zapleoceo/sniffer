"""Public catalogue rollout is an explicit, reviewable compose contract."""

from pathlib import Path

COMPOSE = Path(__file__).parents[1] / "docker-compose.yml"
DEPLOY = Path(__file__).parents[1] / "infra" / "deploy.sh"


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
