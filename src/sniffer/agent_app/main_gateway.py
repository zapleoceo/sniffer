"""Main agent reads a request-scoped catalogue; only application queues work."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sniffer.agent_app.contracts import CollectionScope, MainIdentity, tool
from sniffer.agents.contracts import ToolSpec
from sniffer.bot.store import Sessions
from sniffer.config import get_settings
from sniffer.db.engine import session_scope
from sniffer.db.repositories.agent_requests import AgentRequestRepository
from sniffer.db.repositories.catalog_observations import CatalogObservationRepository
from sniffer.db.repositories.collection_tasks import CollectionTaskRepository
from sniffer.domain.passport import Currency, Passport, counterpart_deal_type

UNSUPPORTED_BUDGET = (
    "Каталог пока умеет строго проверять бюджет только в VND и USD. "
    "Укажите бюджет в одной из этих валют."
)


def _unsupported_budget(passport: Passport) -> bool:
    return passport.budget.currency in {Currency.EUR, Currency.RUB}


def collection_scope(passport: Passport) -> CollectionScope:
    """Build a stable, non-free-form collection identity from the owned passport."""
    attrs = passport.attributes
    canonical = {
        "city": passport.city,
        "category": passport.category,
        "deal_type": counterpart_deal_type(passport.intent),
        "districts": sorted(passport.districts),
        "budget": passport.budget.model_dump(mode="json"),
        "attributes": attrs,
        "must_have": sorted(passport.must_have),
        "deal_breakers": sorted(passport.deal_breakers),
    }
    key = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    category = passport.category.value if passport.category is not None else None
    # Chotot has a verified category code only for motorbikes. Other categories
    # use the city-bound original-message archive instead of a mixed public feed.
    deal_type = counterpart_deal_type(passport.intent)
    sources = (
        ("chotot", "archive")
        if category == "motorbike"
        and deal_type == "sell"
        and passport.budget.currency in (None, Currency.VND, Currency.USD)
        else ("archive",)
    )
    return CollectionScope.model_validate(
        {
            "city": passport.city,
            "category": category,
            "deal_type": deal_type,
            "sources": sources,
            "criteria": {
                "key": key,
                "brand": attrs.get("brand"),
                "model": attrs.get("model"),
                "transmission": attrs.get("transmission"),
                "engine_cc": attrs.get("engine_cc"),
                "engine_cc_dir": attrs.get("engine_cc_dir"),
                "rooms": attrs.get("rooms"),
                "furnished": attrs.get("furnished"),
                "districts": sorted(passport.districts),
                "must_have": sorted(passport.must_have),
                "deal_breakers": sorted(passport.deal_breakers),
                "budget_min": int(passport.budget.min) if passport.budget.min is not None else None,
                "budget_max": int(passport.budget.max) if passport.budget.max is not None else None,
                "budget_currency": passport.budget.currency,
            },
        }
    )


class MainGateway:
    specs: tuple[ToolSpec, ...] = (
        tool(
            "catalog_search", "Read current request's verified catalogue; filters are server-owned."
        ),
        tool("catalog_coverage", "Read collection freshness for the current request."),
    )

    def __init__(self, identity: MainIdentity, sessions: Sessions = session_scope) -> None:
        self.identity, self.sessions = identity, sessions
        self.rows: list[dict[str, Any]] = []

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments or name not in {spec.name for spec in self.specs}:
            raise PermissionError("tool_not_allowed")
        async with self.sessions() as session:
            request = await AgentRequestRepository(session).owned(
                self.identity.user_id, self.identity.request_id, self.identity.version
            )
            p = request.passport
            scope = collection_scope(p)
            if _unsupported_budget(p):
                self.rows = []
                if name == "catalog_coverage":
                    return {"sources": {source: "unsupported" for source in scope.sources}}
                return {"count": 0, "items": []}
            repo = CatalogObservationRepository(session)
            if name == "catalog_coverage":
                return await repo.coverage(scope.model_dump(mode="json"))
            maximum = (
                int(p.budget.max)
                if p.budget.max is not None and p.budget.currency == Currency.VND
                else None
            )
            self.rows = await repo.search(
                city=scope.city,
                category=scope.category,
                deal_type=scope.deal_type,
                max_price_vnd=maximum,
                brand=scope.criteria.brand,
                model=scope.criteria.model,
                limit=20,
            )
            # A compact tool result; full source payload never enters the model context.
            return {
                "count": len(self.rows),
                "items": [
                    {"title": row["observation"]["title"], "facts": row["observation"]["facts"]}
                    for row in self.rows[:5]
                ],
            }

    async def queue_if_needed(self) -> str | None:
        """Not a model tool: authenticated application decides one bounded refresh."""
        if not self.identity.allow_collection:
            return None
        if not get_settings().agent_collector_enabled:
            return (
                "Автоматический сборщик сейчас отключён. "
                "Показаны только проверенные данные каталога."
            )
        async with self.sessions() as session:
            request = await AgentRequestRepository(session).owned(
                self.identity.user_id, self.identity.request_id, self.identity.version
            )
            p = request.passport
            if _unsupported_budget(p):
                return UNSUPPORTED_BUDGET
            scope = collection_scope(p).model_dump(mode="json")
            coverage = await CatalogObservationRepository(session).coverage(scope)
            if all(state == "fresh" for state in coverage["sources"].values()):
                return None
            tasks = CollectionTaskRepository(session)
            current = await tasks.status_for(
                self.identity.user_id, self.identity.request_id, self.identity.version
            )
            pending = next(
                (row for row in current if row["status"] in {"pending", "running"}), None
            )
            if pending:
                state = "выполняется" if pending["status"] == "running" else "в очереди"
                return f"Обновление №{pending['id']}: {state}."
            now = datetime.now(UTC)
            task_id = await tasks.enqueue(
                scope,
                user_id=self.identity.user_id,
                request_id=self.identity.request_id,
                request_version=self.identity.version,
                window_key=now.strftime("%Y-%m-%dT%H"),
            )
            await session.commit()
            return (
                f"Обновление каталога поставлено в очередь (№{task_id}). "
                "Сборщик проверяет очередь раз в час. Повторите этот запрос после обновления; "
                "запуск не гарантирует новых вариантов."
            )
