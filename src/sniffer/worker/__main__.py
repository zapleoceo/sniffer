"""Воркер: превращает Telegram-сырьё в карточки и убирает протухшее.

Коллектор дочитывает историю групп в `raw_messages` каждые пятнадцать минут.
Воркер бесплатно отсекает шум и материализует прошедшие сообщения в
`listings`; уборка остаётся отдельной задачей внутри того же процесса.

Обязательных настроек у воркера нет: `DATABASE_URL` имеет рабочее значение по
умолчанию, а без базы он просто не найдёт задач и уснёт.
"""

from __future__ import annotations

import asyncio

import structlog

from sniffer.config import Settings
from sniffer.runtime.service import Service, idle_loop, run_service
from sniffer.worker.archive import ArchivePipeline
from sniffer.worker.chotot_sync import ChototSync
from sniffer.worker.expiry import Expiry
from sniffer.worker.matcher import Matcher
from sniffer.worker.recategorize import Recategorize
from sniffer.worker.retention import Retention

log = structlog.get_logger(__name__)

NAME = "worker"


def missing_settings(_settings: Settings) -> list[str]:
    return []


async def run(stop: asyncio.Event) -> None:
    log.info("worker.started")
    retention = Retention()
    archive = ArchivePipeline()
    matcher = Matcher()
    chotot = ChototSync()
    expiry = Expiry()
    recategorize = Recategorize()
    await idle_loop(
        stop,
        lambda: _tick(retention, archive, matcher, chotot, expiry, recategorize),
        service=NAME,
    )


async def _tick(
    retention: Retention,
    archive: ArchivePipeline,
    matcher: Matcher,
    chotot: ChototSync,
    expiry: Expiry,
    recategorize: Recategorize,
) -> int:
    """Сколько работы сделали за проход.

    Возврат числа, а не флага, нужен циклу: пока пачки полные, спать незачем.
    """
    # Порядок обязателен: сопоставление обязано видеть карточки, созданные
    # этим же проходом, иначе подписчик узнаёт о находке на четверть часа позже
    # без всякой причины. Доска — тем же процессом и перед сопоставлением по
    # той же причине: карточка Chotot — такая же строка `listings`.
    synced = await chotot.tick()
    processed = await archive.tick()
    # Гашение устаревшего — до сопоставления: подписчику не уходит карточка,
    # которая в этом же проходе перестала быть актуальной.
    expired = await expiry.tick()
    # Пересчёт категорий у накопленного — тоже до сопоставления: подписчику
    # квартиры не уходит карточка, которая этим проходом перестала быть байком.
    expired += await recategorize.tick()
    matched = await matcher.tick()
    return synced + processed + expired + matched + await retention.tick()


SERVICE = Service(name=NAME, requires=missing_settings, run=run)

if __name__ == "__main__":
    run_service(SERVICE)
