from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from aiogram import Bot
from aiogram.methods import AnswerPreCheckoutQuery, SendMessage, SetMyCommands
from aiogram.types import ReplyKeyboardRemove
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.bot import catalog_entry
from sniffer.catalog_mcp.address import MCP_URL
from sniffer.dashboard.app import create_app
from sniffer.db.models import Chat, Listing, Outbox, RawMessage, User
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.db.repositories.public_catalog import PublicCatalogRepository
from sniffer.domain.public_catalog import Filters
from tests import billing_support as tg
from tests.deploy_support import script
from tests.shell_support import function_source, needs_bash, run

HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}
PATH = "/mcp/catalog"
MOMENT = datetime(2026, 11, 21, tzinfo=UTC)


def _call(
    client: TestClient, method: str, params: dict[str, Any] | None = None, **headers: str
) -> Any:
    return client.post(
        PATH,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={**HEADERS, **headers},
    )


def test_mcp_http_discovery_and_protocol_allowlist() -> None:
    with TestClient(create_app(), base_url="https://lbot.zapleo.com") as client:
        init = _call(
            client,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0.1"},
            },
        )
        assert init.status_code == 200
        assert init.json()["result"]["serverInfo"]["name"] == "SnifferBot Public Catalog"
        tools = _call(client, "tools/list").json()["result"]["tools"]
        assert {tool["name"] for tool in tools} == {
            "browse_listings",
            "search_listings",
            "get_listing",
            "catalog_guide",
        }
        assert all(tool["annotations"]["readOnlyHint"] for tool in tools)
        guide = _call(client, "tools/call", {"name": "catalog_guide", "arguments": {}})
        assert "nha_trang" in guide.json()["result"]["structuredContent"]["cities"]
        invalid = _call(
            client,
            "tools/call",
            {
                "name": "browse_listings",
                "arguments": {"limit": 500},
            },
        )
        assert invalid.json()["result"]["isError"]
        missing = _call(client, "tools/call", {"name": "raw_message_get", "arguments": {}})
        assert missing.json()["result"]["isError"]
        assert _call(client, "tools/list", origin="https://wrong.invalid").status_code == 403
        assert _call(client, "tools/list", host="wrong.invalid").status_code == 421
        assert client.post(PATH, content="x" * 17000, headers=HEADERS).status_code == 413
        assert client.get("/database").status_code == 401


async def test_bot_hands_out_mcp_and_rejects_old_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> None:
        raise RuntimeError("test: personal credential storage unavailable")

    monkeypatch.setattr(catalog_entry, "session_scope", unavailable)
    session = tg.RecordingSession()
    bot = tg.make_bot(session)
    dispatcher = catalog_entry.dispatcher()
    for text in ("/start", "/mcp", "/subscription", "Квартира"):
        await dispatcher.feed_update(bot, tg.update(message=tg.message(text=text)))
    messages = session.sent(SendMessage)
    assert len(messages) == 4
    assert all(
        MCP_URL in call.text and isinstance(call.reply_markup, ReplyKeyboardRemove)
        for call in messages
    )
    before = len(session.calls)
    await dispatcher.feed_update(
        bot,
        tg.update(
            pre_checkout_query={
                "id": "old-invoice",
                "from": tg.user(),
                "currency": "XTR",
                "total_amount": 50,
                "invoice_payload": "old",
            }
        ),
    )
    checkout = session.sent(AnswerPreCheckoutQuery)[0]
    assert checkout.ok is False and "/paysupport" in checkout.error_message
    assert len(session.calls) == before + 1


async def test_public_catalog_uses_same_policy_for_page_filters_and_detail(
    db_session: AsyncSession,
) -> None:
    public = Chat(tg_id=101, username="PublicMarket", title="Customer feed", city="nha_trang")
    private = Chat(tg_id=102, username=None, title="Internal", city="nha_trang")
    excluded = Chat(
        tg_id=103,
        username="ExcludedMarket",
        title="Excluded",
        city="nha_trang",
        excluded_at=MOMENT,
    )
    author = Chat(tg_id=104, username="OriginalMarket", title="Author feed", city="nha_trang")
    db_session.add_all([public, private, excluded, author])
    trusted = RawMessage(
        chat_tg_id=101,
        msg_id=39,
        text="Private redaction sentinel",
        text_hash="trusted",
        posted_at=MOMENT,
    )
    invite = RawMessage(
        chat_tg_id=102, msg_id=40, text="Private post", text_hash="invite", posted_at=MOMENT
    )
    excluded_post = RawMessage(
        chat_tg_id=103, msg_id=41, text="Excluded post", text_hash="excluded", posted_at=MOMENT
    )
    pointer = RawMessage(
        chat_tg_id=101,
        msg_id=42,
        text="Pointer",
        origin_kind="pointer",
        text_hash="pointer",
        posted_at=MOMENT,
    )
    forward = RawMessage(
        chat_tg_id=101,
        msg_id=43,
        text="Forwarded from private chat",
        text_hash="forward",
        posted_at=MOMENT,
        origin_kind="forward",
        origin_chat_tg_id=102,
        origin_msg_id=7,
        origin_username="SomePrivateName",
    )
    public_forward = RawMessage(
        chat_tg_id=101,
        msg_id=44,
        text="Public forward",
        text_hash="public-forward",
        posted_at=MOMENT,
        origin_kind="forward",
        origin_chat_tg_id=104,
        origin_msg_id=9,
        origin_username="OriginalMarket",
    )
    db_session.add_all([trusted, invite, excluded_post, pointer, forward, public_forward])
    await db_session.flush()

    def item(
        link: str,
        *,
        source: str = "telegram_archive",
        raw: RawMessage | None = None,
        price: int = 6_000_000,
    ) -> Listing:
        return Listing(
            source=source,
            raw_message_id=raw.id if raw else None,
            external_id=link.split("/")[-1].split(".")[0] if source == "chotot" else link,
            deal_type="rent_out",
            category="apartment",
            city="nha_trang",
            title="secret sentinel name",
            summary="secret sentinel",
            attributes={
                "rooms": 2,
                "balcony": True,
                "kitchen": "separate",
                "phone": "0912345678",
                "model": "secret sentinel",
            },
            price_amount=Decimal(price),
            price_currency="VND",
            price_period="month",
            tg_link=link,
            posted_at=MOMENT,
        )

    safe_tg = item("https://t.me/PublicMarket/39", raw=trusted)
    safe_board = item("https://www.chotot.com/123456.htm", source="chotot", price=7_000_000)
    safe_forward = item("https://t.me/OriginalMarket/9", raw=public_forward, price=7_000_000)
    hidden = [
        item("https://t.me/ImpersonatesPublic/40", raw=invite),
        item("https://t.me/ExcludedMarket/41", raw=excluded_post),
        item("https://t.me/PublicMarket/42", raw=pointer),
        item("https://t.me/SomePrivateName/7", raw=forward),
        item("https://www.chotot.com/234567.htm?redirect=1", source="chotot"),
        item("https://www.chotot.com.example.org/345678.htm", source="chotot"),
    ]
    db_session.add_all([safe_tg, safe_board, *hidden])
    await db_session.flush()
    repo = PublicCatalogRepository(db_session, cities={"nha_trang"}, models={"vision"})
    first = await repo.page(Filters(), before_id=None, limit=1)
    assert [card.id for card in first.items] == [safe_board.id]
    assert first.items[0].source_evidence == "marketplace_post"
    assert first.next_before_id == safe_board.id
    second = await repo.page(Filters(), before_id=first.next_before_id, limit=1)
    assert [card.id for card in second.items] == [safe_tg.id]
    assert second.items[0].source_evidence == "telegram_observed"
    assert second.next_before_id is None
    filtered = await repo.page(
        Filters(max_price_vnd=6_500_000, min_rooms=2, balcony=True, separate_kitchen=True),
        before_id=None,
        limit=10,
    )
    assert filtered.items == second.items
    detail = await repo.detail(safe_tg.id)
    assert detail is not None
    assert detail == second.items[0]
    assert all(word not in detail.model_dump_json() for word in ("secret", "0912345678", "seller"))
    for row in hidden:
        assert await repo.detail(row.id) is None

    db_session.add(safe_forward)
    await db_session.flush()
    strict = await repo.page(Filters(source_reference_only=True), before_id=None, limit=10)
    assert [(c.id, c.source_evidence) for c in strict.items] == [
        (safe_forward.id, "telegram_forward_origin"),
        (safe_board.id, "marketplace_post"),
    ]
    verified = await repo.detail(safe_forward.id)
    assert verified == strict.items[0]
    assert strict.items[0].url == "https://t.me/OriginalMarket/9"
    assert (
        await repo.page(
            Filters(source_reference_only=True, max_price_vnd=6_500_000), before_id=None, limit=10
        )
    ).items == []


async def test_keyset_browse_has_no_hundred_item_ceiling(db_session: AsyncSession) -> None:
    cards = [
        Listing(
            source="chotot",
            external_id=str(n),
            deal_type="sell",
            category="motorbike",
            city="nha_trang",
            title="bike",
            summary="bike",
            tg_link=f"https://chotot.com/{n}.htm",
            posted_at=MOMENT,
        )
        for n in range(1, 132)
    ]
    db_session.add_all(cards)
    await db_session.flush()
    repo = PublicCatalogRepository(db_session, cities={"nha_trang"}, models=set())
    cursor = None
    seen: list[int] = []
    while True:
        found = await repo.page(Filters(), before_id=cursor, limit=50)
        seen.extend(item.id for item in found.items)
        cursor = found.next_before_id
        if cursor is None:
            break
        assert len(seen) <= len(cards)
    assert seen == sorted([card.id for card in cards], reverse=True)


@needs_bash
def test_billing_cutover_guard_blocks_non_pass_and_never_prints_database_details() -> None:
    source = script()
    assert source.index("\ncatalog_billing_gate\n") < source.index("git fetch --prune")
    assert source.index("\ncatalog_billing_gate\n", source.index('log "запуск"')) < (
        source.index("up -d --remove-orphans", source.index('log "запуск"'))
    )
    function = function_source(source, "catalog_billing_gate")
    assert "BEGIN READ ONLY" in function and "ROLLBACK" in function
    for table in ("subscriptions", "payments", "billing_events"):
        assert f"FROM {table}" in function
    for response, result, code in (("pass", 0, 0), ("private_xyz", 0, 41), ("", 1, 41)):
        outcome = run(
            f"""
docker() {{
    cat >/dev/null
    printf '%s\\n' '{response}'
    echo private_xyz >&2
    return {result}
}}
info() {{ echo "$1"; }}
die() {{ echo "$1" >&2; exit "$2"; }}
{function}
catalog_billing_gate
"""
        )
        assert outcome and outcome.code == code and "private_xyz" not in outcome.text


async def test_billing_gate_uses_verified_paid_period_or_unclosed_renewal(
    db_session: AsyncSession,
) -> None:
    from datetime import timedelta

    from sqlalchemy import text
    from sqlalchemy.sql import func

    from sniffer.db.models import BillingEvent, Payment, Subscription

    now = datetime.now(UTC)
    gate = function_source(script(), "catalog_billing_gate")
    stmt = text("SELECT CASE WHEN" + gate.split("SELECT CASE WHEN", 1)[1].split(";", 1)[0])

    async def result() -> str:
        await db_session.flush()
        return str(await db_session.scalar(stmt))

    user = User(tg_user_id=55)
    db_session.add(user)
    await db_session.flush()
    assert await result() == "pass"

    watch = Subscription(user_id=user.id, passport_root=100, expires_at=now + timedelta(days=4))
    db_session.add(watch)
    assert await result() == "pass"
    watch.charge_id = "verified-charge"
    assert await result() == "block"
    watch.charge_id = None

    payment = Payment(
        user_id=user.id,
        amount=50,
        kind="renewal",
        invoice_payload="legacy-invoice",
        external_id="verified-charge",
        is_recurring=True,
        period_end=now + timedelta(days=4),
        created_at=now - timedelta(days=10),
    )
    db_session.add(payment)
    assert await result() == "block"
    payment.period_end = now - timedelta(days=1)
    assert await result() == "block"
    payment.status = "refunded"
    assert await result() == "block", "refund alone does not prove cancellation of recurrence"

    cancellation = BillingEvent(
        tg_user_id=user.tg_user_id,
        kind="sub_canceled",
        payload={"invoice_payload": "legacy-invoice"},
        created_at=func.now(),
    )
    db_session.add(cancellation)
    assert await result() == "pass"
    cancellation.kind = "sub_active"
    assert await result() == "block"
    cancellation.kind = "renewal_canceled"
    cancellation.payload = {}
    cancellation.charge_id = "legacy-invoice"
    assert await result() == "pass"


async def test_live_delivery_reads_and_locks_only_owner_rows(db_session: AsyncSession) -> None:
    owner = User(tg_user_id=301)
    client = User(tg_user_id=302)
    db_session.add_all([owner, client])
    await db_session.flush()
    owner_message = Outbox(user_id=owner.id, payload={}, scheduled_at=MOMENT)
    client_message = Outbox(user_id=client.id, payload={}, scheduled_at=MOMENT)
    db_session.add_all([owner_message, client_message])
    await db_session.flush()
    repo = DeliveryRepository(db_session, owner_only=owner.tg_user_id)
    assert [row.id for row in await repo.take_pending(now=MOMENT)] == [owner_message.id]
    locked = await repo.lock_pending([owner_message.id, client_message.id], now=MOMENT)
    assert [row.id for row in locked] == [owner_message.id]
    no_owner = DeliveryRepository(db_session, owner_only=0)
    assert await no_owner.take_pending(now=MOMENT) == []


async def test_late_settled_receipt_only_journals_and_does_not_activate_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sniffer.bot import catalog_legacy
    from sniffer.config import Settings
    from sniffer.domain.billing import PaymentKind

    ledger = tg.FakeLedger([])
    monkeypatch.setattr(catalog_legacy, "DbLedger", lambda: ledger)
    monkeypatch.setattr(catalog_legacy, "get_settings", lambda: Settings(owner_chat_id=0))
    session = tg.RecordingSession()
    bot = tg.make_bot(session)
    late_receipt = tg.update(
        message=tg.message(
            successful_payment={
                "currency": "XTR",
                "total_amount": 10,
                "invoice_payload": "old",
                "telegram_payment_charge_id": "prior-charge",
                "provider_payment_charge_id": "old",
                "subscription_expiration_date": 1800000000,
                "is_recurring": True,
            }
        )
    )
    await catalog_entry.dispatcher().feed_update(bot, late_receipt)
    await catalog_entry.dispatcher().feed_update(bot, late_receipt)
    assert ledger.payments["prior-charge"].kind is PaymentKind.UNKNOWN
    assert ledger.payments["prior-charge"].is_recurring
    assert len(session.calls) == 1 and isinstance(session.calls[0], SendMessage)
    assert "Слежение не включено" in session.calls[0].text


async def test_production_entry_uses_only_catalog_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sniffer.bot import __main__ as module

    session = tg.RecordingSession()
    monkeypatch.setattr(catalog_entry, "Bot", lambda *_args, **_kwargs: tg.make_bot(session))
    stop = asyncio.Event()
    finished = asyncio.Event()

    class Polling:
        async def start_polling(self, bot: Bot, **kwargs: object) -> None:
            assert set(kwargs) == {"handle_signals", "close_bot_session"}
            [menu] = session.sent(SetMyCommands)
            assert [item.command for item in menu.commands] == ["start", "mcp", "paysupport"]
            stop.set()
            await finished.wait()

        async def stop_polling(self) -> None:
            finished.set()

    monkeypatch.setattr(catalog_entry, "dispatcher", Polling)
    assert vars(module)["SERVICE"] is catalog_entry.SERVICE
    await catalog_entry.SERVICE.run(stop)
    assert finished.is_set()
