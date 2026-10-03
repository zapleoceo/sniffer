"""Сквозная обвязка: настоящий диспетчер aiogram, подставной Bot API, диалог на подделках.

Апдейты — настоящие `Update` (как по проводу), бот — настоящий `Bot` с сессией, которая
записывает каждый вызов как `TelegramMethod`. Всё, что в боевом процессе ходит в Postgres
(отметка доступности, аккаунт квоты, меню поисков, предел поисков), заменено подделкой на
границе МОДУЛЯ, а не хендлера: сами хендлеры, фильтры, `CallbackData`, квота, листание и
диалог — настоящие. Что делает SQL, проверяют тесты с живой базой.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetMe, SendMessage
from aiogram.types import InlineKeyboardMarkup, Update

from sniffer.bot import app as bot_app
from sniffer.bot import query_menu, reachability, watch_flow
from sniffer.bot.billing_payments import PaymentDesk
from sniffer.bot.billing_service import BillingService
from sniffer.bot.billing_support import SupportDesk
from sniffer.bot.billing_telegram import AiogramBotApi
from sniffer.bot.conversation import Conversation, Found
from sniffer.bot.handlers import billing as billing_handlers
from sniffer.bot.handlers import search
from sniffer.bot.quota import Account, QuotaService
from sniffer.bot.watch_panel import PanelView, limit_text
from sniffer.domain.billing import PaymentKind
from sniffer.domain.clarify import ClarificationPlanner
from sniffer.domain.passport import Passport
from sniffer.domain.plans import search_cap
from sniffer.search.intake_rules import parse_query
from sniffer.simulation.ledger import MemoryLedger
from sniffer.simulation.stubs import MemoryStore
from sniffer.sources.base import RawItem
from tests import billing_support as fx
from tests.quota_support import Clock
from tests.test_bot_dialog import FakeJournal

T0 = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)
FRESH = datetime.now(UTC) - timedelta(days=1)
USER = 42
OWNER = fx.OWNER


class Rules:
    """Разбор без модели: ровно тот путь, по которому бот идёт при молчащем брокере."""

    async def parse(self, text: str) -> Passport:
        return parse_query(text, default_city="nha_trang")


def lot(number: int, brand: str = "honda", model: str | None = "vision", **raw: Any) -> RawItem:
    attrs: dict[str, Any] = {"brand": brand}
    if model:
        attrs["model"] = model
    return RawItem(
        source="archive",
        external_id=f"ext-{number}",
        url=f"https://t.me/c/1/{number}",
        title=f"{brand.title()} {model or ''} {number}".replace("  ", " "),
        price_raw="25.000.000 đ",
        price_vnd=25_000_000,
        posted_at=FRESH,
        raw={"listing_id": number, "attributes": attrs, **raw},
    )


def lots(first: int, count: int, **kw: Any) -> list[RawItem]:
    return [lot(n, **kw) for n in range(first, first + count)]


class E2ESession(fx.RecordingSession):
    """Сессия, которая умеет ещё и `getMe`: команды вида `/plan@RecVNbot` проверяют имя бота."""

    async def make_request(self, bot: Bot, method: Any, timeout: int | None = None) -> Any:  # noqa: ASYNC109
        if isinstance(method, GetMe):
            self.calls.append(method)
            me = {"id": 4242, "is_bot": True, "first_name": "Rec", "username": "RecVNbot"}
            body = json.dumps({"ok": True, "result": me})
            return self.check_response(bot=bot, method=method, status_code=200, content=body).result
        return await super().make_request(bot, method, timeout)


class BillingLedger(fx.FakeLedger):
    """Журнал оплаты, из которого выводится число действующих подписок, как в базе."""

    def live_now(self, tg_user_id: int, now: datetime) -> int:
        payloads = {
            r.invoice_payload
            for charge, r in self.payments.items()
            if r.tg_user_id == tg_user_id
            and charge not in self.refunded
            and r.kind in (PaymentKind.FIRST, PaymentKind.RENEWAL)
            and (r.period_end is None or r.period_end > now)
        }
        return max(len(payloads), self.live)

    async def live_subscriptions(self, tg_user_id: int, now: datetime) -> int:
        await self._enter("live_subscriptions")
        return self.live_now(tg_user_id, now)


class Entitle:
    """Число подписок: журнал оплаты, а `forced` — когда тесту журнал не нужен."""

    def __init__(self, bill: BillingLedger, clock: Clock) -> None:
        self.bill, self.clock, self.forced = bill, clock, 0

    def count(self, tg_user_id: int) -> int:
        return max(self.forced, self.bill.live_now(tg_user_id, self.clock()))

    async def slots(self, account: Account, now: datetime) -> int:
        return max(self.forced, self.bill.live_now(account.tg_user_id, now))


class FlowLimit:
    """Предел поисков 1 / 10 на тех же числах и тексте, что у боевого `DbSearchLimit`."""

    def __init__(self, store: MemoryStore, paid: Entitle) -> None:
        self._store, self._paid = store, paid

    def _tg_of(self, user_id: int) -> int:
        return next(tg for tg, uid in self._store._users.items() if uid == user_id)

    def view(self, user_id: int) -> PanelView:
        roots = {r.root for r in self._store.rows if r.user_id == user_id}
        paid = self._paid.count(self._tg_of(user_id))
        return PanelView([], paid, 0, len(roots), search_cap(paid))

    def view_of_tg(self, tg_user_id: int) -> PanelView:
        uid = self._store._users.get(tg_user_id, -1)
        roots = {r.root for r in self._store.rows if r.user_id == uid}
        paid = self._paid.count(tg_user_id)
        return PanelView([], paid, 0, len(roots), search_cap(paid))

    async def blocked(self, user_id: int) -> str | None:
        view = self.view(user_id)
        return None if view.used < view.cap else limit_text(view)


@dataclass
class Flow:
    bot: Bot
    session: fx.RecordingSession
    dispatcher: Dispatcher
    store: MemoryStore
    ledger: MemoryLedger
    clock: Clock
    quota: QuotaService
    talk: Conversation
    paid: Entitle
    limit: FlowLimit
    bill: BillingLedger
    slots: fx.FakeSlots
    found: list[RawItem] = field(default_factory=list)
    source_error: BaseException | None = None
    update_id: int = 100
    sales_enabled: bool = True

    async def _find(self, passport: Passport) -> Found:
        if self.source_error is not None:
            raise self.source_error
        return Found(items=_select(self.found, passport))

    async def feed(self, update: Update) -> list[Any]:
        """Скормить апдейт диспетчеру; вернуть вызовы Bot API, появившиеся за него."""
        before = len(self.session.calls)
        await self.dispatcher.feed_update(self.bot, update)
        return self.session.calls[before:]

    def _next(self) -> int:
        self.update_id += 1
        return self.update_id

    async def say(self, text: str, user: int = USER) -> list[Any]:
        return await self.feed(fx.update(self._next(), message=fx.message(from_id=user, text=text)))

    async def command(self, text: str, user: int = USER) -> list[Any]:
        return await self.feed(fx.command(text, from_id=user, update_id=self._next()))

    async def tap(self, data: str, user: int = USER) -> list[Any]:
        return await self.feed(fx.callback(data, from_id=user, update_id=self._next()))

    async def tap_stale(self, data: str, user: int = USER) -> list[Any]:
        return await self.feed(fx.stale_callback(data, from_id=user, update_id=self._next()))

    async def voice(self, duration: int = 5, user: int = USER) -> list[Any]:
        body = {"file_id": "f1", "file_unique_id": "u1", "duration": duration}
        update = fx.update(self._next(), message=fx.message(from_id=user, voice=body))
        return await self.feed(update)


def _select(items: list[RawItem], passport: Passport) -> list[RawItem]:
    """Отбор как у поиска: названное другое уходит, неизвестное остаётся."""
    want = passport.attributes
    return [
        i
        for i in items
        if all(
            key not in want or i.raw.get("attributes", {}).get(key) in (None, want[key])
            for key in ("brand", "model")
        )
    ]


def texts(calls: list[Any]) -> list[str]:
    """Тексты ответов бота: новые сообщения и правки."""
    return [str(c.text) for c in calls if isinstance(c, SendMessage | EditMessageText)]


def messages(calls: list[Any]) -> list[SendMessage]:
    return [c for c in calls if isinstance(c, SendMessage)]


def buttons(call: Any) -> list[tuple[str, str | None, str | None]]:
    """(подпись, callback_data, url) всех инлайн-кнопок сообщения."""
    markup = getattr(call, "reply_markup", None)
    if not isinstance(markup, InlineKeyboardMarkup):
        return []
    return [(b.text, b.callback_data, b.url) for row in markup.inline_keyboard for b in row]


def button(calls: list[Any], label: str) -> str:
    """callback_data кнопки, чья подпись содержит `label`, в последнем сообщении, где она есть."""
    for call in reversed(calls):
        for text, data, _ in buttons(call):
            if label in text and data:
                return data
    raise AssertionError(f"кнопки «{label}» нет; есть: {[buttons(c) for c in calls]}")


def answered(calls: list[Any]) -> list[AnswerCallbackQuery]:
    return [c for c in calls if isinstance(c, AnswerCallbackQuery)]


def build(monkeypatch: pytest.MonkeyPatch, *, planner: bool = True) -> Iterator[Flow]:
    session = E2ESession()
    bot = fx.make_bot(session)
    clock, ledger, store = Clock(T0), MemoryLedger(), MemoryStore()
    bill, slots = BillingLedger(), fx.FakeSlots()
    paid = Entitle(bill, clock)
    quota = QuotaService(ledger, entitlements=paid, clock=clock, owner_tg_id=OWNER)
    limit = FlowLimit(store, paid)
    holder: list[Flow] = []

    async def find(passport: Passport) -> Found:
        return await holder[0]._find(passport)

    talk = Conversation(
        store,
        intake=lambda: Rules(),
        finder=find,
        recorder=FakeJournal(),
        quota=quota,
        planner=ClarificationPlanner() if planner else None,
        search_limit=limit,
    )
    flow = Flow(
        bot,
        session,
        bot_app.build_dispatcher(),
        store,
        ledger,
        clock,
        quota,
        talk,
        paid,
        limit,
        bill,
        slots,
    )
    holder.append(flow)

    def desks(for_bot: Bot) -> billing_handlers.Desks:
        api = AiogramBotApi(for_bot)
        return billing_handlers.Desks(
            sales=BillingService(
                ledger=bill,
                api=api,
                owner_id=OWNER,
                sales_enabled=flow.sales_enabled,
                nonce=lambda: fx.NONCE,
            ),
            payments=PaymentDesk(
                ledger=bill, api=api, slots=slots, owner_id=OWNER, reply_hours=48, clock=clock
            ),
            support=SupportDesk(ledger=bill, api=api, owner_id=OWNER, reply_hours=48),
        )

    async def list_for(client: Any) -> query_menu.Menu:
        dialogue = await store.load(client)
        return query_menu.Menu(items=await store.live_threads(dialogue))

    async def get_one(client: Any, root: int) -> Any:
        menu = await list_for(client)
        return next((i for i in menu.items if i.root == root), None)

    async def no_record(*_a: Any, **_k: Any) -> None:
        return None

    async def account(client: Any, *_a: Any, **_k: Any) -> Account:
        # Тот же внутренний id, под которым хранилище держит этого клиента: иначе квота диалога
        # и квота `/plan` смотрели бы в разные журналы.
        uid = store._users.setdefault(client.tg_user_id, len(store._users) + 1)
        return Account(user_id=uid, tg_user_id=client.tg_user_id)

    async def can_open(client: Any) -> tuple[bool, PanelView]:
        # Как настоящая панель: со списком поисков, из него отказ берёт корень для «Заменить».
        return await view_with_items(client)

    async def view_with_items(client: Any) -> tuple[bool, PanelView]:
        view = limit.view_of_tg(client.tg_user_id)
        items = await store.live_threads(await store.load(client))
        return view.used < view.cap, PanelView(items, view.paid_slots, 0, view.used, view.cap)

    async def panel(client: Any, **_k: Any) -> PanelView:
        return (await view_with_items(client))[1]

    async def select_ok(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr(reachability, "record", no_record)
    monkeypatch.setattr(search, "conversation", lambda: talk)
    monkeypatch.setattr(search, "quota", lambda: quota)
    monkeypatch.setattr(search, "account_of", account)
    monkeypatch.setattr(watch_flow, "can_open_new", can_open)
    monkeypatch.setattr(watch_flow, "panel", panel)
    monkeypatch.setattr(query_menu, "select", select_ok)
    monkeypatch.setattr(query_menu, "list_for", list_for)
    monkeypatch.setattr(query_menu, "get_one", get_one)
    monkeypatch.setattr(billing_handlers, "desks", desks)
    billing_handlers._issued.clear()
    yield flow
    billing_handlers._issued.clear()
