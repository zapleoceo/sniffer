"""Manual help and bookkeeping for already-issued Telegram receipts.

This module never creates invoices, activates subscriptions, or calls a provider to change
a payment. Historical billing code is not wired into the new dispatcher.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import Bot
from aiogram.filters import CommandObject
from aiogram.types import BotSubscriptionUpdated, Message, Update

from sniffer.bot.billing_guard import Flow
from sniffer.bot.billing_ledger import DbLedger
from sniffer.bot.billing_support import SupportDesk
from sniffer.bot.billing_telegram import AiogramBotApi
from sniffer.config import get_settings
from sniffer.domain.billing import BillingEvent, EventKind, PaymentKind, PaymentRecord

RECEIPT_SAVED = (
    "Платёж за прежнюю подписку зафиксирован. Слежение не включено. "
    "По этому платежу напишите в /paysupport."
)
RECEIPT_MISSING = "Не удалось записать старый платёж. Напишите в /paysupport для ручной сверки."


async def support(message: Message, command: CommandObject, bot: Bot) -> None:
    user = message.from_user
    if user is None:
        return
    settings = get_settings()
    desk = SupportDesk(
        ledger=DbLedger(),
        api=AiogramBotApi(bot),
        owner_id=settings.owner_chat_id,
        reply_hours=settings.paysupport_reply_hours,
    )
    reply = await desk.handle(
        command="paysupport", tg_user_id=user.id, username=user.username, text=command.args or ""
    )
    await message.answer(reply)


async def record_receipt(message: Message, bot: Bot) -> None:
    payment = message.successful_payment
    if payment is None:
        return
    payer = message.from_user.id if message.from_user else message.chat.id
    ledger, flow = DbLedger(), Flow("catalog_legacy_receipt")

    async def save() -> bool:
        expiration = payment.subscription_expiration_date
        return await ledger.record_payment(
            PaymentRecord(
                charge_id=payment.telegram_payment_charge_id,
                tg_user_id=payer,
                amount=payment.total_amount,
                currency=payment.currency,
                # These receipts always wait for manual review; they confer no entitlement.
                kind=PaymentKind.UNKNOWN,
                invoice_payload=payment.invoice_payload,
                is_recurring=bool(payment.is_recurring),
                is_first_recurring=bool(payment.is_first_recurring),
                period_end=datetime.fromtimestamp(expiration, tz=UTC) if expiration else None,
                raw={},
            )
        )

    saved = await flow.step("journal", save)
    owner_id = get_settings().owner_chat_id
    if owner_id and (not saved.ok or saved.value):
        context = (
            "Получен поздний платёж за прежнюю подписку. "
            "Проверьте журнал платежей и проведите ручную сверку."
        )
        await flow.step("alert_owner", lambda: bot.send_message(owner_id, context))
    flow.finish()
    if not saved.ok:
        await message.answer(RECEIPT_MISSING)
    elif saved.value:
        await message.answer(RECEIPT_SAVED)


async def record_provider_refund(message: Message, event_update: Update) -> None:
    refund = message.refunded_payment
    if refund is None:
        return
    ledger, flow = DbLedger(), Flow("catalog_legacy_refunded")
    charge = refund.telegram_payment_charge_id
    event = BillingEvent(
        EventKind.REFUNDED,
        message.from_user.id if message.from_user else message.chat.id,
        {"invoice_payload": refund.invoice_payload, "total_amount": refund.total_amount},
        charge_id=charge,
        update_id=event_update.update_id,
    )
    await flow.step("journal", lambda: ledger.record_event(event))
    await flow.step("mark_existing_record", lambda: ledger.mark_refunded(charge))
    flow.finish()


async def record_provider_status(change: BotSubscriptionUpdated, event_update: Update) -> None:
    types = {
        "canceled": EventKind.SUB_CANCELED,
        "active": EventKind.SUB_ACTIVE,
        "failed": EventKind.SUB_FAILED,
    }
    event = BillingEvent(
        types.get(change.state, EventKind.SUB_OTHER),
        change.user.id,
        {"invoice_payload": change.invoice_payload, "state": change.state},
        update_id=event_update.update_id,
    )
    flow = Flow("catalog_legacy_state")
    await flow.step("journal", lambda: DbLedger().record_event(event))
    flow.finish()
