from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from aiogram.methods import SendMessage
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sniffer.bot import catalog_entry
from sniffer.catalog_mcp import watch_tools
from sniffer.catalog_mcp.address import MCP_URL, PERSONAL_URL
from sniffer.dashboard import app as dashboard
from sniffer.dashboard.app import create_app
from sniffer.db.models import Chat, Listing, RawMessage
from sniffer.db.repositories.catalog_watches import CatalogWatchRepository, fingerprint
from sniffer.domain.catalog_watches import WatchFilters
from sniffer.notifier import catalog_watches
from tests import billing_support as tg

PERSONAL_PATH = "/mcp/personal"
HEADERS = {"accept": "application/json, text/event-stream"}
OWNER = 154321
OTHER = 154322
HOUSE = {
    "city": "nha_trang",
    "category": "house",
    "deal_type": "rent_out",
    "min_rooms": 2,
    "min_price_vnd": 7000000,
    "max_price_vnd": 14000000,
    "price_period": "month",
    "districts": ["hon_xen", "vinh_hai"],
}


def _request(name: str, **arguments: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def test_public_catalog_is_still_free_but_personal_tools_require_a_key() -> None:
    with TestClient(create_app(), base_url="https://lbot.zapleo.com") as client:
        response = client.post(PERSONAL_PATH, json=_request("list_watches"), headers=HEADERS)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        public = client.post("/mcp/catalog", json=_request("list_watches"), headers=HEADERS)
        assert public.status_code == 200
        assert public.json()["result"]["isError"]


def test_watch_districts_are_validated_and_normalized() -> None:
    assert WatchFilters.model_validate(HOUSE | {"districts": ["vinh_hai", "hon_xen", "hon_xen"]})
    assert WatchFilters.model_validate(HOUSE).districts == ["hon_xen", "vinh_hai"]
    with pytest.raises(ValueError, match="district or districts"):
        WatchFilters.model_validate(HOUSE | {"district": "vinh_hai"})
    with pytest.raises(ValueError, match="Unknown district"):
        WatchFilters.model_validate(HOUSE | {"districts": ["not_a_district"]})
    with pytest.raises(ValueError):
        WatchFilters.model_validate(HOUSE | {"source_reference_only": False})
    with pytest.raises(ValueError, match="at least one district"):
        WatchFilters.model_validate(HOUSE | {"districts": []})


async def _telegram_connection(user_id: int) -> str:
    recording = tg.RecordingSession()
    bot = tg.make_bot(recording)
    router = catalog_entry.dispatcher()
    await router.feed_update(bot, tg.update(message=tg.message(from_id=user_id, text="/mcp")))
    [reply] = recording.sent(SendMessage)
    assert reply.chat_id == user_id
    assert MCP_URL in reply.text and PERSONAL_URL in reply.text
    match = re.search(r"Bearer (snw_[A-Za-z0-9_-]{43})", reply.text)
    assert match is not None
    return match[1]


async def test_watch_personal_mcp_to_telegram_with_real_db(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def isolated() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    for module in (dashboard, watch_tools, catalog_entry, catalog_watches):
        monkeypatch.setattr(module, "session_scope", isolated)

    async with sessions.begin() as session:
        for table in (
            "catalog_watch_delivered",
            "catalog_watches",
            "catalog_watch_credentials",
        ):
            await session.execute(text(f"DELETE FROM {table}"))  # noqa: S608 – literal allowlist
        forgery = tg.RecordingSession()
        sender = tg.make_bot(forgery)
        group = tg.message(from_id=OWNER, text="/mcp")
        group["chat"] = {"id": -42, "type": "group"}
        await catalog_entry.dispatcher().feed_update(sender, tg.update(message=group))
        assert not forgery.sent(SendMessage)
        session.add(_listing(123, moment=datetime.now(UTC) - timedelta(days=1)))

    owner_key = await _telegram_connection(OWNER)
    other_key = await _telegram_connection(OTHER)
    invalid_key = "snw_" + "a" * 43
    link = "https://www.chotot.com/456.htm"
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://lbot.zapleo.com"
        ) as client,
    ):

        async def call(key: str, name: str, **args: Any) -> httpx.Response:
            return await client.post(
                PERSONAL_PATH,
                json=_request(name, **args),
                headers={**HEADERS, "Authorization": f"Bearer {key}"},
            )

        assert (await call(invalid_key, "list_watches")).status_code == 401
        bad = (await call(owner_key, "create_watch", filters=HOUSE | {"city": "oops"})).json()
        assert bad["result"]["isError"]
        bad = await call(owner_key, "create_watch", filters=HOUSE | {"tg_chat_id": OTHER})
        assert bad.json()["result"]["isError"]
        created = (await call(owner_key, "create_watch", filters=HOUSE)).json()
        watch_id = created["result"]["structuredContent"]["id"]
        repeat = await call(owner_key, "create_watch", filters=HOUSE)
        assert repeat.json()["result"]["structuredContent"]["id"] == watch_id
        intersect = await call(owner_key, "create_watch", filters=HOUSE | {"min_rooms": 1})
        other_watch_id = intersect.json()["result"]["structuredContent"]["id"]
        assert (await call(other_key, "list_watches")).json()["result"]["structuredContent"] == {
            "result": []
        }
        stop_other = (await call(other_key, "stop_watch", watch_id=watch_id)).json()
        assert stop_other["result"]["structuredContent"] == {"result": False}
        next_key = await _telegram_connection(OWNER)
        assert (await call(owner_key, "list_watches")).status_code == 401
        reads = (await call(next_key, "list_watches")).json()
        assert [r["id"] for r in reads["result"]["structuredContent"]["result"]] == [
            watch_id,
            other_watch_id,
        ]
        sends: list[tuple[int, str]] = []

        async def send(chat: int, message: str) -> None:
            sends.append((chat, message))

        assert await catalog_watches.tick(send)
        assert await catalog_watches.tick(send)
        assert sends == []
        async with sessions.begin() as session:
            moment = datetime.now(UTC)
            session.add_all(
                (
                    _listing(456, moment=moment),
                    _listing(457, moment=moment, price=15000000),
                    _listing(458, moment=moment, price=6000000),
                    _listing(459, moment=moment, district="phuoc_long"),
                )
            )
            session.add(Chat(tg_id=789, username="Group789", title="Observed", city="nha_trang"))
            raw = RawMessage(
                chat_tg_id=789, msg_id=1, text="Copied post", text_hash="x", posted_at=moment
            )
            session.add(raw)
            await session.flush()
            observed = _listing(460, moment=moment)
            observed.source = "telegram_archive"
            observed.raw_message_id = raw.id
            observed.tg_link = "https://t.me/Group789/1"
            session.add(observed)
            await session.execute(text("UPDATE catalog_watches SET next_check_at = now()"))
        assert await catalog_watches.tick(send)
        assert await catalog_watches.tick(send)
        assert [chat for chat, _ in sends] == [OWNER]
        assert link in sends[0][1] and "14 000 000" in sends[0][1]
        assert "Прямая ссылка" in sends[0][1]
        stop = (await call(next_key, "stop_watch", watch_id=watch_id)).json()
        assert stop["result"]["structuredContent"] == {"result": True}
        stop = (await call(next_key, "stop_watch", watch_id=other_watch_id)).json()
        assert stop["result"]["structuredContent"] == {"result": True}
        assert not await catalog_watches.tick(send)
        async with sessions() as session:
            repository = CatalogWatchRepository(session)
            assert await repository.authenticate(next_key) == OWNER
            result = await session.execute(
                text("SELECT token_hash FROM catalog_watch_credentials WHERE tg_chat_id = :chat"),
                {"chat": OWNER},
            )
            assert result.scalar_one() == fingerprint(next_key)


def _listing(
    number: int, *, moment: datetime, price: int = 14_000_000, district: str = "hon_xen"
) -> Listing:
    return Listing(
        source="chotot",
        external_id=str(number),
        tg_link=f"https://www.chotot.com/{number}.htm",
        deal_type="rent_out",
        category="house",
        city="nha_trang",
        district=district,
        title="House",
        summary="Synthetic test",
        price_amount=Decimal(price),
        price_currency="VND",
        price_period="month",
        attributes={"rooms": 2},
        posted_at=moment,
    )
