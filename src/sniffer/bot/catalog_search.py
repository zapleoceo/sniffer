"""Ответ из собственного каталога: паспорт → SQL по `listings` → карточки.

Путь, на котором клиент не ждёт ни модели, ни чужих сайтов. База пополняется
непрерывно и без него — MTProto-коллектор читает чаты каждые пятнадцать минут,
воркер материализует карточки, обход Chotot (`worker/chotot_sync.py`) приносит
доску, — а здесь по ней только ищут. Владелец 12.09.2026: «агент должен сразу
дать ответ, а не уходить в живой поиск по группам».

Устройство нарочно то же, что у живого поиска, только без модели: один план из
одной задачи архивному источнику, `with_vnd_budget` для долларового бюджета,
`run_plan` и тот же `rank_items`. Второго отбора выдачи в проекте быть не
должно — иначе каталог и живой поиск однажды ответили бы на один паспорт
разными карточками. LLM-guard (`verifier.screen`) здесь не зовётся: карточки
каталога уже прошли гейт и дедуп конвейера, а гард стоил 7–17 секунд на запрос
(журнал 04.09.2026) — ровно то ожидание, ради снятия которого путь и существует.
"""

from __future__ import annotations

import structlog

from sniffer.bot import journal
from sniffer.bot.conversation import Found
from sniffer.domain.passport import Currency, Passport
from sniffer.search.currency import usd_vnd_rate
from sniffer.search.live import run_plan
from sniffer.search.plan import TOP_PRIORITY, SearchPlan, SearchTask, context_params
from sniffer.search.relevance import rank_items, with_vnd_budget
from sniffer.sources.archive import SOURCE_NAME as ARCHIVE

log = structlog.get_logger(__name__)


async def find_catalog(passport: Passport) -> Found:
    """Карточки из `listings` по паспорту.

    Ошибка базы у архивного источника — это `degraded` и пустая выдача, а не
    трейсбек: контракт адаптера тот же, что у живых источников.
    """
    watch = journal.Stopwatch()
    # Курс нужен только долларовому бюджету: без него потолок в SQL не ставится
    # и `rank_items` его тоже не применит — честнее показать всё, чем сузить по
    # выдуманному курсу.
    rate = await usd_vnd_rate() if passport.budget.currency is Currency.USD else None
    plan = with_vnd_budget(catalog_plan(passport), passport, rate)
    watch.lap("plan_ms")
    items = rank_items(passport, await run_plan(plan), usd_vnd=rate)
    watch.lap("search_ms")
    log.info("catalog.found", items=len(items))
    return Found(
        items=items,
        sources=tuple(sorted({item.source for item in items})),
        stages=watch.stages,
    )


def catalog_plan(passport: Passport) -> SearchPlan:
    """План из одной задачи: архивный источник ищет полями, текст ему не нужен."""
    return SearchPlan.from_tasks(
        [SearchTask(source=ARCHIVE, query="", priority=TOP_PRIORITY)],
        reasoning="собственный каталог: одна задача архивному источнику",
        defaults=context_params(passport),
    )
