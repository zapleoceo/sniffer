"""PostgreSQL smoke for the complete catalogue answer path.

The broker and exchange rate are deterministic, while request ownership,
catalogue publication/search, MCP transport and card rendering stay real.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sniffer.agent_app import main, main_gateway
from sniffer.agent_app.contracts import MainIdentity
from sniffer.agent_app.main_gateway import MainGateway, collection_scope
from sniffer.bot import cards, conversation
from sniffer.bot.catalog_finder import CatalogFinder
from sniffer.bot.conversation import Conversation, Found, Reply
from sniffer.bot.store import Client, PassportStore
from sniffer.broker.client import BrokerResult
from sniffer.config import Settings
from sniffer.db.repositories.catalog_observations import CatalogObservationRepository
from sniffer.db.repositories.collection_tasks import CollectionTaskRepository
from sniffer.domain.catalog import CatalogFacts, CatalogObservation, Evidence
from sniffer.domain.passport import Category, Passport
from sniffer.search.intake_rules import parse_query

QUERY = "Honda Lead automatic Nha Trang under 500 USD"


class RulesIntake:
    async def parse(self, text: str) -> Passport:
        return parse_query(text)


class Replies:
    def __init__(self) -> None:
        self.sent: list[Reply] = []

    async def __call__(self, reply: Reply) -> None:
        self.sent.append(reply)


class SilentJournal:
    async def open_request(self, *_: object, **__: object) -> None:
        return None

    async def log_answer(self, *_: object, **__: object) -> None:
        return None

    async def close_request(self, *_: object, **__: object) -> None:
        return None


class ToolCallingBroker:
    """One native tool turn proves that ReadAgent uses the in-memory MCP server."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self.options: list[dict[str, object]] = []
        self.closed = False

    async def chat(
        self,
        messages: list[dict[str, Any]],
        **options: object,
    ) -> BrokerResult:
        self.calls.append(deepcopy(messages))
        self.options.append(deepcopy(options))
        if len(self.calls) == 1:
            return BrokerResult(
                text="",
                finish_reason="tool_calls",
                tool_calls=[
                    {
                        "id": "catalog",
                        "type": "function",
                        "function": {"name": "catalog_search", "arguments": "{}"},
                    },
                    {
                        "id": "coverage",
                        "type": "function",
                        "function": {"name": "catalog_coverage", "arguments": "{}"},
                    },
                ],
            )
        return BrokerResult(text="Catalog read.", finish_reason="stop")

    async def aclose(self) -> None:
        self.closed = True


def _observation(
    external_id: str,
    title: str,
    price_vnd: int,
    *,
    brand: str,
    model: str,
    transmission: Literal["automatic", "manual", "semi"],
    engine_cc: int,
) -> CatalogObservation:
    price = f"{price_vnd:,}".replace(",", " ")
    raw = (
        f"Nha Trang. {title}. For sale. Price {price} VND. Available. "
        f"brand={brand}; model={model}; transmission={transmission}; engine={engine_cc}."
    )
    facts = CatalogFacts(
        city="nha_trang",
        category=Category.MOTORBIKE,
        deal_type="sell",
        price_vnd=price_vnd,
        active=True,
        brand=brand,
        model=model,
        transmission=transmission,
        engine_cc=engine_cc,
    )
    return CatalogObservation(
        source="chotot",
        external_id=external_id,
        url=f"https://www.chotot.com/{external_id}.htm",
        fetched_at=datetime.now(UTC) - timedelta(seconds=30),
        posted_at=datetime.now(UTC) - timedelta(hours=2),
        title=title,
        raw_text=raw,
        extractor_version="postgres-smoke-v1",
        facts=facts,
        evidence=(
            Evidence(field="city", quote="Nha Trang"),
            Evidence(field="category", quote=title),
            Evidence(field="deal_type", quote="For sale"),
            Evidence(field="price_vnd", quote=f"{price} VND"),
            Evidence(field="active", quote="Available"),
            Evidence(field="brand", quote=brand),
            Evidence(field="model", quote=model),
            Evidence(field="transmission", quote=transmission),
            Evidence(field="engine_cc", quote=str(engine_cc)),
        ),
    )


@pytest.mark.asyncio
async def test_exact_catalogue_request_reaches_postgres_without_redundant_llm_and_renders_card(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def session_factory() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    settings = Settings.model_validate(
        {"catalog_mode": "catalog", "agent_collector_enabled": True, "max_cards": 5}
    )
    broker = ToolCallingBroker()
    gateway_trace: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    class TrackingGateway:
        specs = MainGateway.specs

        def __init__(self, identity: MainIdentity) -> None:
            self._gateway: MainGateway = MainGateway(identity, session_factory)
            self.sessions = session_factory

        @property
        def rows(self) -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = self._gateway.rows
            return rows

        async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            result: dict[str, Any] = await self._gateway.call(name, arguments)
            gateway_trace.append((name, deepcopy(arguments), deepcopy(result)))
            return result

        async def queue_if_needed(self) -> str | None:
            status: str | None = await self._gateway.queue_if_needed()
            return status

    monkeypatch.setattr(main, "BrokerClient", lambda **_: broker)
    monkeypatch.setattr(main, "MainGateway", lambda identity: TrackingGateway(identity))
    monkeypatch.setattr(main, "usd_vnd_rate", lambda: _rate())
    monkeypatch.setattr(main_gateway, "get_settings", lambda: settings)
    monkeypatch.setattr(conversation, "get_settings", lambda: settings)
    monkeypatch.setattr(cards, "get_settings", lambda: settings)

    store = PassportStore(session_factory)
    client = Client(tg_user_id=9_000_000_001, username="postgres_smoke")
    seeded_identity: tuple[int, int, int] | None = None
    observations = (
        _observation(
            "postgres-smoke-lead",
            "Honda Lead 125 automatic",
            8_900_000,
            brand="honda",
            model="lead",
            transmission="automatic",
            engine_cc=125,
        ),
        _observation(
            "postgres-smoke-wrong-model",
            "Honda Vision 110 automatic",
            8_000_000,
            brand="honda",
            model="vision",
            transmission="automatic",
            engine_cc=110,
        ),
        _observation(
            "postgres-smoke-wrong-gearbox",
            "Honda Lead 125 manual",
            8_500_000,
            brand="honda",
            model="lead",
            transmission="manual",
            engine_cc=125,
        ),
        _observation(
            "postgres-smoke-over-budget",
            "Honda Lead 125 automatic expensive",
            15_000_000,
            brand="honda",
            model="lead",
            transmission="automatic",
            engine_cc=125,
        ),
    )
    unpublished = _observation(
        "postgres-smoke-unpublished",
        "Honda Lead 125 automatic unpublished",
        8_700_000,
        brand="honda",
        model="lead",
        transmission="automatic",
        engine_cc=125,
    )

    async def seeded_catalog(
        user_id: int, request_id: int, version: int, *, allow_collection: bool
    ) -> main.CatalogAnswer:
        nonlocal seeded_identity
        assert allow_collection and seeded_identity is None
        dialogue = await store.load(client)
        assert dialogue.user_id == user_id and dialogue.passport is not None
        assert (dialogue.passport.root, dialogue.passport.version) == (request_id, version)
        seeded_identity = (user_id, request_id, version)
        scope = collection_scope(dialogue.passport.passport).model_dump(mode="json")
        async with session_factory() as session:
            tasks = CollectionTaskRepository(session)
            task_id = await tasks.enqueue(
                scope,
                user_id=user_id,
                request_id=request_id,
                request_version=version,
                window_key="postgres-smoke",
            )
            lease = await tasks.claim()
            assert lease is not None and lease.id == task_id
            catalog = CatalogObservationRepository(session)
            for observation in observations:
                observation_id = await catalog.stage(task_id, lease.token, observation)
                assert await catalog.publish(task_id, lease.token, observation_id)
            await catalog.stage(task_id, lease.token, unpublished)
            for source in scope["sources"]:
                await catalog.record_coverage(task_id, lease.token, source, "success")
            await tasks.complete(task_id, lease.token, {"published": len(observations)})
            await session.commit()
        return await main.search_request(
            user_id, request_id, version, allow_collection=allow_collection
        )

    legacy_calls = 0

    async def forbidden_legacy(_passport: Passport) -> Found:
        nonlocal legacy_calls
        legacy_calls += 1
        raise AssertionError("catalog smoke reached legacy search")

    replies = Replies()
    talk = Conversation(
        store,
        intake=RulesIntake,
        finder=forbidden_legacy,
        scoped_finder=CatalogFinder(
            legacy=forbidden_legacy,
            catalog=seeded_catalog,
            settings=lambda: settings,
        ),
        recorder=SilentJournal(),
    )
    await talk.on_text(client, QUERY, replies)

    dialogue = await store.load(client)
    assert dialogue.passport is not None
    assert seeded_identity == (dialogue.user_id, dialogue.passport.root, 1)
    assert dialogue.passport.version == 1 and legacy_calls == 0
    assert broker.calls == [] and not broker.closed
    assert [name for name, _, _ in gateway_trace] == ["catalog_search"]
    rendered = replies.sent[-1]
    assert rendered.feedback and rendered.offer_subscription
    assert "<b>Honda Lead 125 automatic</b>" in rendered.text
    assert "8 900 000 VND" in rendered.text
    assert 'href="https://www.chotot.com/postgres-smoke-lead.htm"' in rendered.text
    for unwanted in ("wrong-model", "wrong-gearbox", "over-budget", "unpublished"):
        assert unwanted not in rendered.text
    assert "Обновление" not in rendered.text
    assert "fake" not in rendered.text.lower()

    async with session_factory() as session:
        tasks = await CollectionTaskRepository(session).status_for(
            dialogue.user_id, dialogue.passport.root, dialogue.passport.version
        )
    assert len(tasks) == 1 and tasks[0]["status"] == "done"


async def _rate() -> float:
    return 25_000.0
