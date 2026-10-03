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
from collections.abc import Awaitable, Callable, Sequence

import structlog

from sniffer.config import Settings
from sniffer.runtime.service import Service, idle_loop, run_service
from sniffer.search.currency import usd_vnd_rate
from sniffer.worker.archive import ArchivePipeline
from sniffer.worker.chotot_sync import ChototSync
from sniffer.worker.enrich_cli import EXIT_OK, EXIT_USAGE, run_enrich
from sniffer.worker.expiry import Expiry
from sniffer.worker.monitor import MonitorAgent
from sniffer.worker.quota_sweep import ReservationSweep
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


def build_monitor() -> MonitorAgent:
    """Агент слежения со всем, что ему нужно снаружи.

    Курс — зависимость, которую агенту ДАЮТ: в тесте он собирается без сети. Но и забыть её
    здесь нельзя: агент без источника курса держит каждый слот с долларовым бюджетом в
    ожидании вечно. Прежний `Matcher()` без курса молча не сужал бюджет вовсе (D2: у 10 из 14
    бюджетных паспортов в базе бюджет в USD, замер 03.10.2026), и ни один модульный тест этого
    не видел — дефект сидел в проводке, а не в самом матчере. Поэтому проводка вынесена в
    функцию, и её проверяет отдельный тест.
    """
    return MonitorAgent(rate=usd_vnd_rate)


# Как часто агент слежения смотрит, не появилось ли новое. Секунды, а не 15 минут коллектора:
# пока в базе нет карточек новее курсора слота, проход стоит один запрос `max(id)`, а когда
# появились — клиент получает их «как только попало в базу», а не в следующий цикл воронки.
MONITOR_POLL_S = 5.0
# Пауза после сбоя прохода агента: «упал — залогировал — подождал — снова», а не падение
# всего процесса вместе с воронкой.
MONITOR_RETRY_S = 30.0


async def guarded(tick: Callable[[], Awaitable[int]], *, name: str) -> int:
    """Проход, который не роняет задачу: сбой в журнал, работа продолжается.

    Охрана до `Exception`, не до `BaseException`: остановка процесса (`CancelledError`) —
    не сбой, и глотать её нельзя. Сам слот агента уже изолирован своим SAVEPOINT; здесь
    ловится то, что вне слотов (база недоступна, `claim_due`), и оно не должно ронять
    воронку, которая живёт в соседней задаче того же процесса.
    """
    try:
        return await tick()
    except Exception as exc:
        log.error("worker.task_failed", task=name, error=f"{type(exc).__name__}: {exc}"[:300])
        await asyncio.sleep(MONITOR_RETRY_S)
        return 0


async def run(stop: asyncio.Event) -> None:
    log.info("worker.started")
    retention = Retention()
    archive = ArchivePipeline()
    monitor = build_monitor()
    chotot = ChototSync()
    expiry = Expiry()
    recategorize = Recategorize()
    screening = Screening()
    reservations = ReservationSweep()
    # Две независимые задачи одного процесса: воронка (источники, гашение, ИИ-проверка) и
    # агент слежения. ИИ-проверка держит проход до двух минут на пачку, и в одной цепочке
    # `await` она задерживала бы слежение; сбой одного, в свою очередь, не должен
    # останавливать другого.
    await asyncio.gather(
        idle_loop(
            stop,
            lambda: _tick(
                retention, archive, chotot, expiry, recategorize, screening, reservations
            ),
            service=NAME,
        ),
        idle_loop(
            stop,
            lambda: guarded(monitor.tick, name="monitor"),
            service="monitor",
            poll_interval_s=MONITOR_POLL_S,
        ),
    )


async def _tick(
    retention: Retention,
    archive: ArchivePipeline,
    chotot: ChototSync,
    expiry: Expiry,
    recategorize: Recategorize,
    screening: Screening,
    reservations: ReservationSweep,
) -> int:
    """Сколько работы сделали за проход воронки.

    Возврат числа, а не флага, нужен циклу: пока пачки полные, спать незачем.
    """
    # Порядок обязателен: гашение и пересчёт — после источников, чтобы видеть
    # карточки этого же прохода. Доска — тем же процессом и перед гашением по
    # той же причине: карточка Chotot — такая же строка `listings`.
    synced = await chotot.tick()
    processed = await archive.tick()
    # Гашение устаревшего: слежение (отдельная задача) не ставит в очередь карточку, которая
    # уже перестала быть актуальной, — `is_active` читается самим запросом отбора.
    expired = await expiry.tick()
    # Пересчёт категорий у накопленного: слот судит карточку по её текущей категории.
    expired += await recategorize.tick()
    # Проверка моделью: слежение не берёт карточку из архива Telegram, пока вердикта нет
    # (`first_unready_id`), — позиция в этой цепочке больше ничего не гарантирует, гарантия
    # теперь данные, а не порядок вызовов.
    expired += await screening.tick()
    # Резервы показов снимаются независимо от воронки: это не карточки, а слоты квоты
    # людей, которым сообщение не дошло, и ждать конца воронки им незачем.
    swept = await reservations.tick()
    return synced + processed + expired + swept + await retention.tick()


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
