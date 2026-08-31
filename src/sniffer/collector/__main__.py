"""Коллектор: единственный держатель MTProto-сессии.

На P0 сессии ещё нет, и это нормальное состояние, а не сбой: процесс говорит,
каких настроек ждёт, и ждёт. Догон истории по `chats.last_msg_id` и
live-подписка приходят на P1 — их место в `_tick`.

Юзербот только читает (CLAUDE.md). Ни одного исходящего действия отсюда не
появится: личный аккаунт ловит `PEER_FLOOD` за пять сообщений незнакомым
людям, а аккаунт здесь один.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence

import structlog

from sniffer.collector.auth import run_auth
from sniffer.config import Settings
from sniffer.runtime.service import Service, idle_loop, run_service

log = structlog.get_logger(__name__)

NAME = "collector"

# Разовая ручная операция живёт подкомандой того же модуля, а не отдельным
# скриптом: авторизация нужна ровно тому образу, в котором потом крутится
# коллектор, и версия Telethon у них обязана быть одна.
AUTH_COMMAND = "auth"

# Интервал опроса больше, чем у воркера: Telegram не любит частых обращений, а
# новые сообщения приходят подпиской, а не поллингом.
POLL_INTERVAL_S = 15.0


def missing_settings(settings: Settings) -> list[str]:
    """Без строки сессии авторизоваться в контейнере всё равно негде.

    Интерактивный ввод кода подтверждения в фоновом процессе невозможен,
    поэтому `TG_SESSION` такое же обязательное требование, как ключи
    приложения. Строку выдаёт подкоманда `auth` — разовая ручная операция.
    """
    required = {
        "TG_API_ID": bool(settings.tg_api_id),
        "TG_API_HASH": bool(settings.tg_api_hash.strip()),
        "TG_SESSION": bool(settings.tg_session.strip()),
    }
    return [name for name, filled in required.items() if not filled]


async def run(stop: asyncio.Event) -> None:
    log.info("collector.started")
    await idle_loop(stop, _tick, service=NAME, poll_interval_s=POLL_INTERVAL_S)


async def _tick() -> int:
    """Сколько сообщений забрали за проход. P1: догон истории и live-подписка."""
    return 0


SERVICE = Service(name=NAME, requires=missing_settings, run=run)


def main(argv: Sequence[str]) -> int:
    """Без аргументов — обычный процесс; `auth` — разовая авторизация."""
    if argv and argv[0] == AUTH_COMMAND:
        return run_auth()
    run_service(SERVICE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
