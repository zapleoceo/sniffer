"""Общие заготовки тестов оплаты: сессия-заглушка, настоящие апдейты, поддельные порты.

Отдельным файлом, а не копией в каждом тесте: пять файлов про оплату собирают одно и то же
окружение, и две копии заглушки разошлись бы на первой правке.

Заглушка сессии — НЕ класс с `**kwargs`. Она записывает каждый вызов как настоящий
`TelegramMethod` aiogram, и любое поле вне схемы метода видно в `method.model_extra` (на
этом держится контрактный тест). Фейк, принимающий любые аргументы, зеленел на ровно той
ошибке, которую надо ловить: `answer_invoice(**kwargs)` пропускал `subscription_period`,
которого у `sendInvoice` нет.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import (
    CreateInvoiceLink,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import Update

from sniffer.domain.billing import (
    PAID,
    REFUNDED,
    REFUNDING,
    BillingEvent,
    EventKind,
    PaymentKind,
    PaymentRecord,
    StarTransaction,
    StoredPayment,
)
from sniffer.domain.slots import SlotState

OWNER = 169510539
CLIENT = 42
LINK = "https://t.me/$test-invoice-link"
NONCE = "0123456789ab"


class RecordingSession(BaseSession):
    """Записывает каждый вызов Bot API и отвечает заготовкой, как настоящий сервер."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        # Что бросить на вызов метода этого класса: ошибка сети, отказ Telegram.
        self.failures: dict[type[TelegramMethod[Any]], BaseException] = {}

    async def close(self) -> None:
        return None

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 - сигнатура абстрактного метода aiogram
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b""

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 - сигнатура абстрактного метода aiogram
    ) -> Any:
        self.calls.append(method)
        failure = self.failures.get(type(method))
        if failure is not None:
            raise failure
        body = json.dumps({"ok": True, "result": _result_for(method)})
        return self.check_response(bot=bot, method=method, status_code=200, content=body).result

    def sent(self, kind: type[TelegramMethod[Any]]) -> list[Any]:
        return [call for call in self.calls if isinstance(call, kind)]


def _result_for(method: TelegramMethod[Any]) -> Any:
    if isinstance(method, CreateInvoiceLink):
        return LINK
    if isinstance(method, SendMessage | EditMessageText):
        return {"message_id": 1, "date": 0, "chat": {"id": CLIENT, "type": "private"}}
    if isinstance(method, EditMessageReplyMarkup):
        return True
    return True


def make_bot(session: RecordingSession | None = None) -> Bot:
    """Бот с теми же умолчаниями, что в бою (`bot/app.py`): HTML и без превью ссылок."""
    return Bot(
        "42:TEST",
        session=session or RecordingSession(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


def session_of(bot: Bot) -> RecordingSession:
    assert isinstance(bot.session, RecordingSession)
    return bot.session


# ── настоящие апдейты: то, что Telegram присылает по проводу ─────────────────


def user(user_id: int = CLIENT) -> dict[str, Any]:
    return {"id": user_id, "is_bot": False, "first_name": "Дима", "username": "dima"}


def message(*, from_id: int = CLIENT, message_id: int = 10, **fields: Any) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "date": 1_700_000_000,
        "chat": {"id": from_id, "type": "private"},
        "from": user(from_id),
        **fields,
    }


def update(update_id: int = 1, **body: Any) -> Update:
    return Update.model_validate({"update_id": update_id, **body})


def command(text: str, *, from_id: int = CLIENT, update_id: int = 1) -> Update:
    entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return update(update_id, message=message(from_id=from_id, text=text, entities=entities))


def pre_checkout(
    payload: str,
    *,
    from_id: int = CLIENT,
    amount: int = 10,
    currency: str = "XTR",
    update_id: int = 2,
) -> Update:
    return update(
        update_id,
        pre_checkout_query={
            "id": "pcq-1",
            "from": user(from_id),
            "currency": currency,
            "total_amount": amount,
            "invoice_payload": payload,
        },
    )


def successful_payment(
    payload: str,
    *,
    charge: str = "charge-1",
    from_id: int = CLIENT,
    amount: int = 10,
    currency: str = "XTR",
    expiration: int | None = 1_900_000_000,
    first: bool = True,
    recurring: bool = True,
    update_id: int = 3,
) -> Update:
    payment: dict[str, Any] = {
        "currency": currency,
        "total_amount": amount,
        "invoice_payload": payload,
        "telegram_payment_charge_id": charge,
        "provider_payment_charge_id": charge,
    }
    if expiration is not None:
        payment["subscription_expiration_date"] = expiration
    if recurring:
        payment["is_recurring"] = True
    if first:
        payment["is_first_recurring"] = True
    return update(update_id, message=message(from_id=from_id, successful_payment=payment))


def refunded_payment(payload: str, *, charge: str = "charge-1", update_id: int = 4) -> Update:
    refund = {
        "currency": "XTR",
        "total_amount": 10,
        "invoice_payload": payload,
        "telegram_payment_charge_id": charge,
    }
    return update(update_id, message=message(refunded_payment=refund))


def subscription_changed(
    payload: str, state: str, *, from_id: int = CLIENT, update_id: int = 5
) -> Update:
    body = {"user": user(from_id), "invoice_payload": payload, "state": state}
    return update(update_id, subscription=body)


def callback(
    data: str, *, from_id: int = CLIENT, message_id: int = 10, update_id: int = 6
) -> Update:
    return update(
        update_id,
        callback_query={
            "id": "cb-1",
            "from": user(from_id),
            "chat_instance": "ci",
            "data": data,
            "message": message(from_id=from_id, message_id=message_id, text="экран"),
        },
    )


def stale_callback(data: str, *, from_id: int = CLIENT, update_id: int = 7) -> Update:
    """Сообщение старше 48 часов: Telegram отдаёт его недоступным (`date` = 0)."""
    stale = {"message_id": 10, "date": 0, "chat": {"id": from_id, "type": "private"}}
    return update(
        update_id,
        callback_query={
            "id": "cb-2",
            "from": user(from_id),
            "chat_instance": "ci",
            "data": data,
            "message": stale,
        },
    )


# ── порты ───────────────────────────────────────────────────────────────────


class Failing(Exception):
    """Чужой тип, о котором код не знает: тест на полноту охраны без списка классов."""


class FakeLedger:
    """Журнал в памяти. Поведение — как у настоящего: уникальность по charge id и по update_id."""

    def __init__(self, order: list[str] | None = None) -> None:
        self.order = order if order is not None else []
        self.payments: dict[str, PaymentRecord] = {}
        self.refunded: set[str] = set()
        self.refunding: set[str] = set()
        # Когда платёж лёг в журнал: по умолчанию давно (сверка не считает его «свежим»).
        self.created: dict[str, datetime] = {}
        self.consents: set[tuple[int, str, str]] = set()
        self.events: list[BillingEvent] = []
        self.live = 0
        self.calls: list[str] = []
        # Что бросить на вызов метода с этим именем и что повесить (зависание базы).
        self.failures: dict[str, BaseException] = {}
        self.hangs: set[str] = set()

    async def _enter(self, name: str) -> None:
        self.calls.append(name)
        self.order.append(f"ledger:{name}")
        if name in self.hangs:
            await asyncio.sleep(3600)
        failure = self.failures.get(name)
        if failure is not None:
            raise failure

    async def record_payment(self, record: PaymentRecord) -> bool:
        await self._enter("record_payment")
        fresh = record.charge_id not in self.payments
        self.payments.setdefault(record.charge_id, record)
        if fresh and any(
            e.kind == EventKind.REFUNDED and e.charge_id == record.charge_id for e in self.events
        ):
            self.refunded.add(record.charge_id)
        return fresh

    async def get_payment(self, charge_id: str) -> StoredPayment | None:
        await self._enter("get_payment")
        record = self.payments.get(charge_id)
        if record is None:
            return None
        return StoredPayment(
            charge_id=record.charge_id,
            tg_user_id=record.tg_user_id,
            amount=record.amount,
            currency=record.currency,
            kind=record.kind.value,
            status=self._status(charge_id),
            invoice_payload=record.invoice_payload,
            is_recurring=record.is_recurring,
            is_first_recurring=record.is_first_recurring,
            period_end=record.period_end,
            refunded_at=None,
            created_at=self.created.get(charge_id, datetime.now(UTC) - timedelta(days=1)),
        )

    def _status(self, charge_id: str) -> str:
        if charge_id in self.refunded:
            return REFUNDED
        return REFUNDING if charge_id in self.refunding else PAID

    async def recent_payments(self, tg_user_id: int, limit: int) -> list[StoredPayment]:
        await self._enter("recent_payments")
        found = [
            await self.get_payment(charge)
            for charge, record in self.payments.items()
            if record.tg_user_id == tg_user_id
        ]
        return [payment for payment in reversed(found) if payment is not None][:limit]

    async def mark_refunding(self, charge_id: str) -> bool:
        await self._enter("mark_refunding")
        changed = charge_id in self.payments and self._status(charge_id) == PAID
        if changed:
            self.refunding.add(charge_id)
        return changed

    async def mark_refunded(self, charge_id: str) -> bool:
        await self._enter("mark_refunded")
        changed = charge_id in self.payments and charge_id not in self.refunded
        if changed:
            self.refunded.add(charge_id)
            self.refunding.discard(charge_id)
        return changed

    async def first_payment_of(self, invoice_payload: str) -> StoredPayment | None:
        await self._enter("first_payment_of")
        for charge, record in self.payments.items():
            if record.invoice_payload == invoice_payload:
                return await self.get_payment(charge)
        return None

    async def payments_since(self, since: datetime) -> list[StoredPayment]:
        await self._enter("payments_since")
        found = [await self.get_payment(charge) for charge in self.payments]
        return [p for p in found if p is not None and p.created_at >= since]

    async def unsettled_refunds(self, older_than: datetime) -> list[StoredPayment]:
        await self._enter("unsettled_refunds")
        found = [await self.get_payment(charge) for charge in self.payments]
        return [
            p
            for p in found
            if p is not None
            and (
                (p.created_at < older_than and p.status == REFUNDING)
                or (
                    p.created_at < older_than
                    and p.status == PAID
                    and p.kind == PaymentKind.UNKNOWN.value
                )
                or (p.status == PAID and await self.has_event(EventKind.REFUNDED, p.charge_id))
                or (
                    p.status == REFUNDED
                    and (
                        not await self.has_event(EventKind.REFUND_SYNCED, p.charge_id)
                        or (
                            (p.is_recurring or p.is_first_recurring)
                            and p.invoice_payload is not None
                            and not await self.has_event(
                                EventKind.RENEWAL_CANCELED, p.invoice_payload
                            )
                        )
                    )
                )
            )
        ]

    async def has_event(self, kind: EventKind, charge_id: str) -> bool:
        await self._enter("has_event")
        return any(e.kind == kind and e.charge_id == charge_id for e in self.events)

    async def first_charge_of(self, invoice_payload: str) -> str | None:
        await self._enter("first_charge_of")
        for charge, record in self.payments.items():
            if record.invoice_payload == invoice_payload:
                return charge
        return None

    async def live_subscriptions(self, tg_user_id: int, now: datetime) -> int:
        await self._enter("live_subscriptions")
        return self.live

    async def record_consent(self, tg_user_id: int, doc: str, version: str) -> None:
        await self._enter("record_consent")
        self.consents.add((tg_user_id, doc, version))

    async def has_consent(self, tg_user_id: int, doc: str, version: str) -> bool:
        await self._enter("has_consent")
        return (tg_user_id, doc, version) in self.consents

    async def record_event(self, event: BillingEvent) -> bool:
        await self._enter("record_event")
        if event.update_id is not None and any(e.update_id == event.update_id for e in self.events):
            return False
        self.events.append(event)
        return True

    async def events_within(self, tg_user_id: int, kind: EventKind, window: timedelta) -> int:
        await self._enter("events_within")
        return sum(1 for e in self.events if e.tg_user_id == tg_user_id and e.kind == kind)


class FakeSlots:
    """`Slots` без базы: записывает пересчёты и отдаёт заданное состояние."""

    def __init__(self, order: list[str] | None = None) -> None:
        self.order = order if order is not None else []
        self.syncs: list[int] = []
        self.state = SlotState(slots=1, holding=0, resumed=0)
        self.failure: BaseException | None = None

    async def sync(self, tg_user_id: int, now: datetime) -> SlotState:
        self.order.append("slots:sync")
        self.syncs.append(tg_user_id)
        if self.failure is not None:
            raise self.failure
        return self.state


class RecordingApi:
    """`BotApi` без Telegram: записывает вызовы и по имени метода бросает заданное."""

    def __init__(self, order: list[str] | None = None) -> None:
        self.links: list[dict[str, Any]] = []
        self.refunds: list[tuple[int, str]] = []
        self.cancels: list[tuple[int, str]] = []
        self.texts: list[tuple[int, str]] = []
        self.failures: dict[str, BaseException] = {}
        self.order = order if order is not None else []
        # Что «лежит» в истории звёзд у Telegram: порядок задаёт тест, отдаётся страницами.
        self.history: list[StarTransaction] = []

    def _enter(self, name: str) -> None:
        self.order.append(f"api:{name}")
        failure = self.failures.get(name)
        if failure is not None:
            raise failure

    async def create_invoice_link(
        self, *, title: str, description: str, payload: str, label: str, amount: int, period_s: int
    ) -> str:
        self._enter("create_invoice_link")
        self.links.append(
            {
                "title": title,
                "description": description,
                "payload": payload,
                "label": label,
                "amount": amount,
                "period_s": period_s,
            }
        )
        return LINK

    async def refund_star_payment(self, *, user_id: int, charge_id: str) -> None:
        self._enter("refund_star_payment")
        self.refunds.append((user_id, charge_id))

    async def cancel_star_subscription(self, *, user_id: int, charge_id: str) -> None:
        self._enter("cancel_star_subscription")
        self.cancels.append((user_id, charge_id))

    async def send_text(self, chat_id: int, text: str) -> None:
        self._enter("send_text")
        self.texts.append((chat_id, text))

    async def star_transactions(self, *, offset: int, limit: int) -> list[StarTransaction]:
        self._enter("star_transactions")
        return self.history[offset : offset + limit]
