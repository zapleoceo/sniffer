"""Жизненный цикл процесса: настройка, сигналы, холостой ход.

Четыре процесса отличаются работой, а не тем, как они живут. Общего у них
ровно три вещи, и каждая уже стоила бы отладки по отдельности.

**Нет секрета — не падаем.** Процесс без обязательной настройки логирует, чего
именно не хватает, и уходит ждать. Контейнер, который перезапускается каждые
пять секунд, выглядит как сломанный деплой и заваливает healthcheck, хотя на
деле не заведён один токен. Ожидание с перепроверкой отличает «не настроен» от
«сломан» — и в логе, и в `docker ps`.

**SIGTERM — это просьба, а не убийство.** `docker compose stop` даёт десять
секунд; процесс, который их не использует, теряет незавершённый апдейт.

**Холостой цикл молчит.** Воркеру нечего делать почти всё время, и если он
скажет об этом на каждой итерации, лог станет бесполезен, а `max-size: 10m` из
compose открутится за час.
"""

from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from sniffer.config import Settings, get_settings, reload_settings
from sniffer.runtime.logs import setup_logging

log = structlog.get_logger(__name__)

# Не чаще раза в минуту: перепроверка ничего не стоит, но лог о ненастроенном
# процессе не должен превращаться в поток.
RECHECK_INTERVAL_S = 60.0
POLL_INTERVAL_S = 5.0
IDLE_LOG_EVERY_S = 300.0

# Сколько работы сделал один проход. Ноль означает «очередь пуста» — цикл
# засыпает, всё остальное продолжает без паузы.
Tick = Callable[[], Awaitable[int]]


@dataclass(slots=True, frozen=True)
class Service:
    """Описание процесса: имя, требования к настройке и сама работа."""

    name: str
    # Возвращает имена ПУСТЫХ обязательных настроек — так, как они названы в
    # `.env`, потому что читать лог будет тот, кто этот `.env` и правит.
    requires: Callable[[Settings], list[str]]
    run: Callable[[asyncio.Event], Awaitable[None]]


def run_service(service: Service) -> None:
    """Единственная точка входа процесса: `__main__.py` вызывает только её."""
    setup_logging(get_settings().log_level)
    try:
        asyncio.run(_bootstrap(service))
    except KeyboardInterrupt:
        # Ctrl+C при локальном запуске — не авария, трейсбек тут только мешает.
        log.info("service.interrupted", service=service.name)


async def _bootstrap(service: Service) -> None:
    stop = asyncio.Event()
    _handle_signals(stop)
    await serve(service, stop)


async def serve(
    service: Service,
    stop: asyncio.Event,
    *,
    recheck_s: float = RECHECK_INTERVAL_S,
) -> None:
    """Ждёт настройки, потом работает, потом тихо заканчивает.

    Событие остановки приходит снаружи: тестам нужен контроль над ним, а
    процессу — общий на все задачи флаг, а не своя копия у каждой.
    """
    log.info("service.starting", service=service.name)
    while not stop.is_set():
        missing = service.requires(reload_settings())
        if not missing:
            await service.run(stop)
            break
        log.warning(
            "service.not_configured",
            service=service.name,
            missing=missing,
            recheck_in_s=recheck_s,
            message=f"не настроен: нет {', '.join(missing)}, жду конфигурации",
        )
        await sleep_or_stop(stop, recheck_s)
    log.info("service.stopped", service=service.name)


async def idle_loop(
    stop: asyncio.Event,
    tick: Tick,
    *,
    service: str,
    poll_interval_s: float = POLL_INTERVAL_S,
    idle_log_every_s: float = IDLE_LOG_EVERY_S,
) -> None:
    """Цикл, в который на P1 вставляется настоящая работа.

    Сделан циклом, а не вечным сном, ровно чтобы вставлять было некуда, кроме
    `tick`: процесс уже умеет засыпать без работы, просыпаться по расписанию и
    останавливаться по сигналу — эти три вещи не придётся писать заново.
    """
    next_idle_log = time.monotonic()
    while not stop.is_set():
        handled = await tick()
        if handled:
            log.info("service.tick", service=service, handled=handled)
            # Очередь могла не опустеть — берём следующую пачку без паузы.
            next_idle_log = time.monotonic() + idle_log_every_s
            continue

        now = time.monotonic()
        if now >= next_idle_log:
            log.info("service.idle", service=service, next_report_in_s=idle_log_every_s)
            next_idle_log = now + idle_log_every_s

        if await sleep_or_stop(stop, poll_interval_s):
            break


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Пауза, прерываемая остановкой. True — попросили остановиться.

    `asyncio.sleep` на минуту означал бы минуту между SIGTERM и завершением,
    то есть гарантированный SIGKILL от Docker.
    """
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


def _handle_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def request_stop(sig: signal.Signals) -> None:
        log.info("service.signal", signal=sig.name)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop, sig)
        except NotImplementedError:
            # Windows: у ProactorEventLoop нет add_signal_handler. Прод —
            # Linux, но локальная отладка обязана останавливаться так же.
            signal.signal(sig, lambda _number, _frame: loop.call_soon_threadsafe(stop.set))
