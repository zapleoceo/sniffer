"""Тестовая среда Telegram: платежи звёздами проверяются там бесплатно, без списаний.

Среда отдельная: свои аккаунты и боты, запросы идут на `/bot<token>/test/<метод>`. В aiogram
для этого есть константа `TEST`; здесь проверяется, что сессию собирает ОДНО место и что ею
пользуются все процессы с `Bot(...)`, — тест-режим, включённый у одного, молча оставлял бы
остальные в боевой среде (docs/payments-live-check.md).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import PRODUCTION, TEST
from pydantic import ValidationError

from sniffer.config import Settings
from sniffer.telegram_env import bot_session

SRC = Path(__file__).resolve().parents[1] / "src" / "sniffer"


def settings(**fields: Any) -> Settings:
    """Без `.env` разработчика: его `TELEGRAM_ENV=test` не должен менять результат теста."""
    return Settings(_env_file=None, **fields)  # type: ignore[call-arg]


def test_production_is_the_default_and_needs_no_session() -> None:
    assert settings().telegram_env == "prod"
    assert not settings().telegram_test_environment
    assert bot_session(settings()) is None, "библиотека возьмёт боевую среду сама"


@pytest.mark.parametrize("value", ["test", "TEST", " Test ", "test\n"])
def test_the_value_test_is_recognised_in_any_case_and_with_stray_whitespace(value: str) -> None:
    assert settings(telegram_env=value).telegram_test_environment


def test_the_test_environment_talks_to_the_test_path_of_the_bot_api() -> None:
    """Сессия создаётся один раз: загрузка сертификатов у `AiohttpSession` стоит ~2 секунды."""
    session = bot_session(settings(telegram_env="test"))

    assert isinstance(session, AiohttpSession) and session.api == TEST
    assert session.api.api_url(token="42:T", method="getMe") == (
        "https://api.telegram.org/bot42:T/test/getMe"
    )
    assert PRODUCTION.api_url(token="42:T", method="getMe") == (
        "https://api.telegram.org/bot42:T/getMe"
    )


@pytest.mark.parametrize("value", ["", "prod", "production", "staging", "tset"])
def test_anything_but_test_stays_in_production(value: str) -> None:
    """Опечатка в значении не уводит боевого бота в тестовую среду: токен там всё равно чужой."""
    assert bot_session(settings(telegram_env=value)) is None


def test_the_environment_variable_has_the_documented_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ENV", "test")

    assert settings().telegram_test_environment


def _bot_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Bot"
    ]


def test_every_process_builds_its_bot_through_the_one_session_factory() -> None:
    """Бот, нотифаер и оповещения коллектора: тест-режим не может включиться у части процессов."""
    calls = {
        f"{path.relative_to(SRC)}:{call.lineno}": call
        for path in SRC.rglob("*.py")
        for call in _bot_calls(path)
    }

    assert len(calls) >= 3, f"найдено {sorted(calls)}: проверка перестала видеть процессы"
    for where, call in calls.items():
        factory = [
            kw.value
            for kw in call.keywords
            if kw.arg == "session"
            and isinstance(kw.value, ast.Call)
            and isinstance(kw.value.func, ast.Name)
            and kw.value.func.id == "bot_session"
        ]
        assert factory, f"{where}: Bot(...) без session=bot_session(settings)"


def test_the_reply_deadline_for_payment_questions_is_a_setting_with_sane_bounds() -> None:
    assert settings().paysupport_reply_hours == 48
    assert settings(paysupport_reply_hours=72).paysupport_reply_hours == 72
    assert settings(paysupport_reply_hours="").paysupport_reply_hours == 48, "пустое — умолчание"
    for bad in (0, -1, 721):
        with pytest.raises(ValidationError):
            settings(paysupport_reply_hours=bad)
