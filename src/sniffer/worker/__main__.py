"""Воркер: превращает Telegram-сырьё в карточки и убирает протухшее.

Коллектор дочитывает историю групп в `raw_messages` каждые пятнадцать минут.
Воркер бесплатно отсекает шум и материализует прошедшие сообщения в
`listings`; уборка остаётся отдельной задачей внутри того же процесса.

Обязательных настроек у воркера нет: `DATABASE_URL` имеет рабочее значение по
умолчанию, а без базы он просто не найдёт задач и уснёт.

Разовая операция живёт подкомандой того же модуля: `python -m sniffer.worker
enrich` — проход догона по накопленным карточкам (`worker/enrich_cli.py`). Ей
нужен ровно тот же образ и тот же код разбора, что у воронки.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence

import structlog

from sniffer.config import Settings
from sniffer.runtime.service import Service, idle_loop, run_service
from sniffer.worker.archive import ArchivePipeline
from sniffer.worker.chotot_sync import ChototSync
from sniffer.worker.enrich_cli import EXIT_OK, EXIT_USAGE, run_enrich
from sniffer.worker.expiry import Expiry
from sniffer.worker.matcher import Matcher
from sniffer.worker.recategorize import Recategorize
from sniffer.worker.retention import Retention
from sniffer.worker.screening import Screening

log = structlog.get_logger(__name__)

NAME = "worker"

ENRICH_COMMAND = "enrich"
USAGE = (
    "Использование: python -m sniffer.worker [enrich [--dry-run] [--limit N] [--since-id ID]]\n"
    "  без аргументов — обычный процесс воркера;\n"
    f"  {ENRICH_COMMAND} — разовый проход догона по накопленным карточкам."
)


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
    screening = Screening()
    await idle_loop(
        stop,
        lambda: _tick(retention, archive, matcher, chotot, expiry, recategorize, screening),
        service=NAME,
    )


async def _tick(
    retention: Retention,
    archive: ArchivePipeline,
    matcher: Matcher,
    chotot: ChototSync,
    expiry: Expiry,
    recategorize: Recategorize,
    screening: Screening,
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
    # Проверка моделью — тоже до сопоставления: подписчику не уходит «обмен
    # валют», который модель этим проходом признала не товаром.
    expired += await screening.tick()
    matched = await matcher.tick()
    return synced + processed + expired + matched + await retention.tick()


SERVICE = Service(name=NAME, requires=missing_settings, run=run)


def main(argv: Sequence[str]) -> int:
    """Без аргументов — обычный процесс; `enrich ...` — разовый проход догона.

    Разбор строгий: **любой первый аргумент, кроме `enrich`, — ошибка, а не
    повод поднять сервис**. Опечатка в подкоманде молча уходила бы в демона:
    аргумент проигнорирован, процесс живёт, владелец ждёт отчёт прохода, которого
    не будет, и видит в логе обычный старт воркера.
    """
    if not argv:
        run_service(SERVICE)
        return EXIT_OK
    if argv[0] == ENRICH_COMMAND:
        return run_enrich(argv[1:])
    print(f"Неизвестная подкоманда: {argv[0]!r}.\n{USAGE}", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
