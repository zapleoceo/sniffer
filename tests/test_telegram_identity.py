"""Подпись сеанса в Telegram → Настройки → Устройства — одна на всех.

Клиента создают два модуля из разных слоёв: `collector/client.py` (разовый вход
за строкой сессии) и `sources/telegram_client.py` (боевое чтение по этой
строке). Telegram считает устройством пару (auth key, подпись клиента), поэтому
один и тот же `StringSession`, поднятый с другим `device_model`, показывается в
списке ВТОРОЙ строкой — как будто вошли заново.

Тест нужен именно потому, что расхождение ничего не ломает: оба клиента
работают, тесты зелёные, лог чист. Его замечают месяцем позже по лишнему
устройству в списке — и уже не понимают, откуда оно.
"""

from __future__ import annotations

import pytest

from sniffer import telegram_identity as identity
from sniffer.collector import client as collector_client
from sniffer.config import Settings
from sniffer.sources import telegram_client as reader_client
from sniffer.sources import telegram_discover_client as joiner_client

IDENTITY_KEYS = ("device_model", "system_version", "app_version")


def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Убрать сеть, оставив боевой путь создания клиента.

    Подменяются ровно `TelegramClient` и `StringSession` — всё остальное в
    обоих модулях работает как в бою, включая `flood_sleep_threshold=0` у
    читателя.
    """
    seen: dict[str, object] = {}

    def fake_client(_session: object, api_id: int, api_hash: str, **kwargs: object) -> object:
        seen.clear()
        seen.update(kwargs)
        return object()

    monkeypatch.setattr("telethon.TelegramClient", fake_client)
    monkeypatch.setattr("telethon.sessions.StringSession", lambda *a: object())
    return seen


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        tg_api_id=42,
        tg_api_hash="hash",
        tg_session="1BQANOTEuMTA4LjU2LjEyOQG7fake",
    )


def test_the_reader_introduces_itself_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Боевой читатель не передавал подпись вовсе — три ключа отсутствовали.

    Именно этот модуль работает в проде; безымянным в «Устройствах» висел он,
    а не разовая команда `auth`.
    """
    seen = _capture(monkeypatch)

    reader_client.new_reader(_settings())

    for key in IDENTITY_KEYS:
        assert seen.get(key), f"{key} не передан: в «Устройствах» появится вторая строка"


def test_both_clients_introduce_themselves_identically(monkeypatch: pytest.MonkeyPatch) -> None:
    """Все способы работы с одной сессией представляются одинаково."""
    seen = _capture(monkeypatch)

    collector_client.new_client(42, "hash")
    from_auth = {key: seen[key] for key in IDENTITY_KEYS}

    reader_client.new_reader(_settings())
    from_reader = {key: seen[key] for key in IDENTITY_KEYS}

    joiner_client.new_joiner(_settings())
    from_joiner = {key: seen[key] for key in IDENTITY_KEYS}

    assert from_auth == from_reader
    assert from_auth == from_joiner
    assert from_auth == identity.IDENTITY, "подпись берётся из одного места, а не копируется"


def test_the_reader_keeps_its_own_flood_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Общая подпись не отменяет того, чем читатель от команды `auth` отличается.

    `flood_sleep_threshold=0` — не украшение: с ним не работают ни растущая
    пауза, ни бюджет источника, ни `degraded`.
    """
    seen = _capture(monkeypatch)

    reader_client.new_reader(_settings())

    assert seen["flood_sleep_threshold"] == 0


def test_the_identity_module_names_the_product() -> None:
    """Подпись должна быть узнаваемой: по ней владелец отзывает сеанс."""
    assert "SnifferBot" in identity.DEVICE_MODEL
    assert set(identity.IDENTITY) == set(IDENTITY_KEYS)
