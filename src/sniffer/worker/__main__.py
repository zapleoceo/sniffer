"""Воркер: разбирает очередь `jobs` и убирает протухшее сырьё.

На P0 очередь `jobs` никто не наполняет, зато сырьё уже течёт: коллектор
дочитывает историю групп в `raw_messages` каждые пятнадцать минут. Поэтому у
воркера появилась первая настоящая работа — уборка по сроку хранения
(`retention.py`), «крон», живущий в коде, а не в crontab сервера.

Обязательных настроек у воркера нет: `DATABASE_URL` имеет рабочее значение по
умолчанию, а без базы он просто не найдёт задач и уснёт.
"""

from __future__ import annotations

import asyncio

import structlog

from sniffer.config import Settings
from sniffer.runtime.service import Service, idle_loop, run_service
from sniffer.worker.retention import Retention

log = structlog.get_logger(__name__)

NAME = "worker"


def missing_settings(_settings: Settings) -> list[str]:
    return []


async def run(stop: asyncio.Event) -> None:
    log.info("worker.started")
    retention = Retention()
    await idle_loop(stop, lambda: _tick(retention), service=NAME)


async def _tick(retention: Retention) -> int:
    """Сколько работы сделали за проход.

    Очередь `jobs` пока никто не наполняет, но уборка сырья нужна уже сейчас:
    `raw_messages` растёт с каждым проходом коллектора, а схема обещала TTL,
    которого не существовало (`infra/sql/001_init.sql`).

    P1: сюда же встаёт `SELECT … FOR UPDATE SKIP LOCKED` по `jobs` и ступени
    воронки. Возврат числа, а не флага, нужен циклу: пока пачки полные, спать
    незачем.
    """
    return await retention.tick()


SERVICE = Service(name=NAME, requires=missing_settings, run=run)

if __name__ == "__main__":
    run_service(SERVICE)
