"""Исполнение плана: задачи → адаптеры → сырые находки.

Здесь тратится время клиента, поэтому здесь же живёт бюджет из spec-v2 (2.3):
не больше пяти запросов к одному источнику и 90 секунд на весь план. Что не
уложилось — отдаётся частично: пять карточек за минуту полезнее двадцати за
десять.

Про источники этот модуль знает ровно два имени — из плана и из реестра.
Добавление адаптера его не касается.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog

from sniffer.search.plan import SearchPlan, SearchTask
from sniffer.sources.base import RawItem, Source, UnknownSourceError, get_source

log = structlog.get_logger(__name__)

# spec-v2, 2.3: потолок запросов на источник и общий таймаут плана.
MAX_TASKS_PER_SOURCE = 5
PLAN_BUDGET_S = 90.0


async def run_plan(plan: SearchPlan, *, budget_s: float = PLAN_BUDGET_S) -> list[RawItem]:
    """Находки без дублей, свежие первыми. Наружу не бросает."""
    by_source = _group(plan.tasks)
    adapters = _adapters(by_source)
    if not adapters:
        return []

    running = {
        asyncio.create_task(_drain(adapter, by_source[name]), name=f"source:{name}"): name
        for name, adapter in adapters.items()
    }
    try:
        items = await _collect(running, budget_s)
    finally:
        # Адаптер держит свой httpx-клиент; незакрытый — это утечка сокетов
        # на каждый запрос клиента.
        await asyncio.gather(*(adapter.aclose() for adapter in adapters.values()))

    unique = _unique(items)
    log.info("live.done", sources=sorted(adapters), found=len(items), unique=len(unique))
    return sorted(unique, key=_freshness, reverse=True)


async def _collect(
    running: dict[asyncio.Task[list[RawItem]], str],
    budget_s: float,
) -> list[RawItem]:
    done, pending = await asyncio.wait(running, timeout=budget_s)
    for task in pending:
        log.warning("live.budget_exceeded", source=running[task], budget_s=budget_s)
        task.cancel()
    # Отменённые задачи надо дождаться до закрытия клиентов, иначе адаптер
    # окажется закрыт под ногами у собственного запроса.
    await asyncio.gather(*pending, return_exceptions=True)

    items: list[RawItem] = []
    for task in done:
        error = task.exception()
        if error is None:
            items.extend(task.result())
            continue
        # Контракт адаптера — не бросать. Если бросил, это баг адаптера, и
        # платить за него выдачей остальных источников клиент не должен.
        log.warning("live.source_crashed", source=running[task], error=str(error))
    return items


async def _drain(adapter: Source, tasks: list[SearchTask]) -> list[RawItem]:
    """Запросы к одному источнику — последовательно.

    Параллелить запросы к одному хосту значит выглядеть как атака и получить
    бан там, где мы и так гости.
    """
    items: list[RawItem] = []
    for task in tasks[:MAX_TASKS_PER_SOURCE]:
        items.extend(await adapter.search(task.query, task.params))
        if adapter.degraded:
            log.warning("live.source_degraded", source=adapter.name, query=task.query)
            break
    return items


def _group(tasks: list[SearchTask]) -> dict[str, list[SearchTask]]:
    grouped: dict[str, list[SearchTask]] = {}
    for task in tasks:
        grouped.setdefault(task.source, []).append(task)
    return grouped


def _adapters(by_source: dict[str, list[SearchTask]]) -> dict[str, Source]:
    adapters: dict[str, Source] = {}
    for name in by_source:
        try:
            adapters[name] = get_source(name)
        except UnknownSourceError:
            # Реестр источников живёт в БД и может опережать код.
            log.warning("live.no_adapter", source=name)
    return adapters


def _unique(items: list[RawItem]) -> list[RawItem]:
    """Один лот, найденный двумя запросами, — одна карточка."""
    seen: set[tuple[str, str]] = set()
    unique: list[RawItem] = []
    for item in items:
        key = (item.source, item.external_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _freshness(item: RawItem) -> datetime:
    """Без метки времени лот уходит в конец: непроверяемая свежесть — не свежесть."""
    posted = item.posted_at
    if posted is None:
        return datetime.min.replace(tzinfo=UTC)
    return posted if posted.tzinfo else posted.replace(tzinfo=UTC)
