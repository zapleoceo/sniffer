"""Воркер: разбирает очередь `jobs`.

На P0 очередь никто не наполняет — сырьё в базу ещё не течёт. Процесс всё
равно поднимается и держит цикл: так видно, что он жив, и так на P1 работа
вставляется в одно место, а не пишется вокруг заново.

Обязательных настроек у воркера нет: `DATABASE_URL` имеет рабочее значение по
умолчанию, а без базы он просто не найдёт задач и уснёт.
"""

from __future__ import annotations

import asyncio

import structlog

from sniffer.config import Settings
from sniffer.runtime.service import Service, idle_loop, run_service

log = structlog.get_logger(__name__)

NAME = "worker"


def missing_settings(_settings: Settings) -> list[str]:
    return []


async def run(stop: asyncio.Event) -> None:
    log.info("worker.started")
    await idle_loop(stop, _tick, service=NAME)


async def _tick() -> int:
    """Сколько задач обработали за проход.

    P1: `SELECT … FOR UPDATE SKIP LOCKED` по `jobs`, затем ступени воронки.
    Возврат числа, а не флага, нужен циклу: пока пачки полные, спать незачем.
    """
    return 0


SERVICE = Service(name=NAME, requires=missing_settings, run=run)

if __name__ == "__main__":
    run_service(SERVICE)
