"""Нотифаер: разбирает `outbox` и шлёт сообщения клиентам.

На P0 подписок ещё нет, очередь пуста. Токен всё равно обязателен: отправляет
он тем же Bot API, что и диалоговый процесс, и без токена доставка невозможна
в принципе — лучше сказать это в логе сразу, чем при первой же карточке.
"""

from __future__ import annotations

import asyncio

import structlog

from sniffer.config import Settings
from sniffer.runtime.service import Service, idle_loop, run_service

log = structlog.get_logger(__name__)

NAME = "notifier"

# Реже воркера: мгновенность здесь не нужна, а троттлинг доставки обязателен —
# бот, шлющий сорок сообщений в день, отключается в первые сутки.
POLL_INTERVAL_S = 10.0


def missing_settings(settings: Settings) -> list[str]:
    return [] if settings.bot_token.strip() else ["BOT_TOKEN"]


async def run(stop: asyncio.Event) -> None:
    log.info("notifier.started")
    await idle_loop(stop, _tick, service=NAME, poll_interval_s=POLL_INTERVAL_S)


async def _tick() -> int:
    """Сколько сообщений доставили за проход. P1: выборка `outbox` по `scheduled_at`."""
    return 0


SERVICE = Service(name=NAME, requires=missing_settings, run=run)

if __name__ == "__main__":
    run_service(SERVICE)
