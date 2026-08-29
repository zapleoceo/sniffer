"""Планировщик поиска: паспорт → план по источникам.

Ровно один вызов LLM на запрос (spec-v2, раздел 2.3). Всё, что модель может
сделать не так — упасть, не ответить, ответить мусором, — приводит к
шаблонному плану, а не к отказу: бот без модели должен искать хуже, но искать.

Источники приходят параметром из реестра в БД. Планировщик не знает ни одного
источника по имени: добавление адаптера не меняет здесь ни строки.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import httpx
import structlog

from sniffer.broker.client import BrokerClient, BrokerError
from sniffer.domain.passport import Passport
from sniffer.search.fallback import fallback_plan
from sniffer.search.plan import SearchPlan, context_params, parse_tasks
from sniffer.search.prompt import SYSTEM_PROMPT, build_user_prompt, plan_schema

log = structlog.get_logger(__name__)

PLAN_MAX_TOKENS = 1200


class StructuredCaller(Protocol):
    """Ровно то, что планировщику нужно от брокера, и ничего сверх."""

    async def structured(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        schema_name: str,
        system: str | None = None,
        max_tokens: int = 2048,
    ) -> dict[str, Any]: ...


class SearchPlanner:
    def __init__(self, broker: StructuredCaller | None = None) -> None:
        # Брокер создаётся лениво: собрать планировщик должно быть можно и без
        # настроенного окружения — конфиг читается только перед вызовом.
        self._broker = broker

    async def plan(self, passport: Passport, available_sources: Sequence[str]) -> SearchPlan:
        sources = list(
            dict.fromkeys(source.strip() for source in available_sources if source.strip())
        )
        if not sources:
            # Пустой реестр — состояние системы, а не сбой планировщика.
            log.warning("planner.no_sources")
            return SearchPlan(reasoning="реестр источников пуст", is_fallback=True)

        try:
            payload = await self._ask(passport, sources)
        except (BrokerError, httpx.HTTPError, TimeoutError) as exc:
            log.warning("planner.broker_failed", kind=type(exc).__name__, error=str(exc))
            return fallback_plan(passport, sources, reason=type(exc).__name__)

        if not isinstance(payload, Mapping):
            log.warning("planner.payload_not_object", kind=type(payload).__name__)
            return fallback_plan(passport, sources, reason="ответ модели не объект")

        tasks = parse_tasks(payload.get("tasks"), sources)
        if not tasks:
            # Схема строгая, но не все провайдеры её соблюдают: пустой или
            # выдуманный план равнозначен отсутствию модели.
            log.warning("planner.no_valid_tasks", keys=sorted(payload))
            return fallback_plan(passport, sources, reason="в ответе модели нет валидных задач")

        plan = SearchPlan.from_tasks(
            tasks,
            reasoning=str(payload.get("reasoning", "")),
            defaults=context_params(passport),
        )
        log.info("planner.ready", tasks=len(plan.tasks), sources=plan.sources())
        return plan

    async def _ask(self, passport: Passport, sources: list[str]) -> dict[str, Any]:
        broker = self._broker or BrokerClient()
        self._broker = broker
        return await broker.structured(
            build_user_prompt(passport, sources),
            schema=plan_schema(sources),
            schema_name="search_plan",
            system=SYSTEM_PROMPT,
            max_tokens=PLAN_MAX_TOKENS,
        )
