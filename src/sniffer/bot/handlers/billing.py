"""Оплата в диалоге: команды, кнопки и платёжные апдейты Telegram.

Тонкий слой над сервисами оплаты: достаёт из апдейта факты, отдаёт их сервису и рисует
ответ. Решений о деньгах здесь нет — они в `billing_service.py` и `billing_payments.py`.

Роутер подключается ДО диалога (`bot/app.py`): `@router.message(F.text)` диалога ловит всё,
в том числе команды, и без этого порядка `/paysupport` уходил бы в поиск как поисковая
фраза — а ответ на неё обязателен по ToS Telegram.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

import structlog
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotSubscriptionUpdated,
    CallbackQuery,
    Message,
    PreCheckoutQuery,
    Update,
)

from sniffer.bot import billing_owner_wording as owner_words
from sniffer.bot import billing_ui as ui
from sniffer.bot import billing_wording as words
from sniffer.bot.billing import (
    CheckoutFacts,
    PaymentFacts,
    RefundedFacts,
    parse_refund_args,
)
from sniffer.bot.billing_ledger import DbLedger
from sniffer.bot.billing_payments import PaymentDesk
from sniffer.bot.billing_service import BillingService, Verdict, guarded_verdict
from sniffer.bot.billing_support import SupportDesk
from sniffer.bot.billing_telegram import AiogramBotApi
from sniffer.bot.keyboards import SubscribeCallback
from sniffer.config import get_settings

log = structlog.get_logger(__name__)

router = Router(name="billing")

# Сколько нажатий «принимаю» помним, чтобы двойной тап не выдал две ссылки (две оплаты
# одной и той же подписки). Память процесса: после рестарта кнопка снова рабочая, а второй
# счёт всё равно придётся оплатить сознательно.
_REMEMBER = 1000
_issued: OrderedDict[tuple[int, int], None] = OrderedDict()


@dataclass(frozen=True, slots=True)
class Desks:
    sales: BillingService
    payments: PaymentDesk
    support: SupportDesk


def desks(bot: Bot) -> Desks:
    """Сервисы оплаты на этот `Bot`. Точка подмены в тестах: порты — подделки."""
    settings = get_settings()
    api, ledger = AiogramBotApi(bot), DbLedger()
    owner, hours = settings.owner_chat_id, settings.paysupport_reply_hours
    return Desks(
        sales=BillingService(ledger=ledger, api=api, owner_id=owner),
        payments=PaymentDesk(ledger=ledger, api=api, owner_id=owner, reply_hours=hours),
        support=SupportDesk(ledger=ledger, api=api, owner_id=owner, reply_hours=hours),
    )


def _claim(key: tuple[int, int]) -> bool:
    """Занять нажатие. Синхронно, без `await` между проверкой и записью: гонки нет."""
    if key in _issued:
        return False
    _issued[key] = None
    while len(_issued) > _REMEMBER:
        _issued.popitem(last=False)
    return True


# ── команды ─────────────────────────────────────────────────────────────────


@router.message(Command("subscription"))
async def subscription_command(message: Message, bot: Bot) -> None:
    if message.from_user is not None:
        await _show_confirmation(message, bot, message.from_user.id)


@router.message(Command("terms"))
async def terms_command(message: Message) -> None:
    await message.answer(words.terms(get_settings().paysupport_reply_hours))


@router.message(Command("paysupport", "support"))
async def support_command(message: Message, command: CommandObject, bot: Bot) -> None:
    if message.from_user is None:
        return
    reply = await desks(bot).support.handle(
        command=command.command,
        tg_user_id=message.from_user.id,
        username=message.from_user.username,
        text=command.args or "",
    )
    await message.answer(reply)


@router.message(Command("refund"))
async def refund_command(message: Message, command: CommandObject, bot: Bot) -> None:
    """Возврат платежа — только владельцу. Остальным команды как бы нет, но и поиском не станет."""
    payments = desks(bot).payments
    if message.from_user is None or not payments.is_owner(message.from_user.id):
        return
    parsed = parse_refund_args(command.args or "")
    if parsed is None:
        await message.answer(owner_words.refund_usage())
        return
    await message.answer(await payments.refund(parsed[0], user_id=parsed[1]))


async def _show_confirmation(message: Message, bot: Bot, tg_user_id: int) -> None:
    screen = await desks(bot).sales.confirmation(tg_user_id)
    await message.answer(
        screen.text, reply_markup=ui.confirmation_markup() if screen.offer else None
    )


# ── кнопки ──────────────────────────────────────────────────────────────────


async def _live_message(callback: CallbackQuery) -> Message | None:
    """Сообщение под кнопкой или явный ответ, что кнопка устарела.

    Сообщение старше 48 часов Telegram отдаёт недоступным. Молча выйти нельзя: на кнопке с
    деньгами человек нажал и ждёт, а получил тишину.
    """
    if isinstance(callback.message, Message):
        return callback.message
    await callback.answer(words.STALE_BUTTON, show_alert=True)
    return None


@router.callback_query(SubscribeCallback.filter())
async def subscribe_button(callback: CallbackQuery, bot: Bot) -> None:
    """Прежняя кнопка «Следить за новыми»: теперь ведёт в безопасный поток `/subscription`.

    Подписка по аккаунту, а не «за тему запроса», поэтому корень поиска из кнопки здесь не
    нужен: подтверждение с цифрами, согласие, ссылка — те же, что у команды.
    """
    message = await _live_message(callback)
    if message is None:
        return
    await callback.answer()
    await _show_confirmation(message, bot, callback.from_user.id)


@router.callback_query(ui.BillingCallback.filter())
async def billing_button(
    callback: CallbackQuery, callback_data: ui.BillingCallback, bot: Bot
) -> None:
    message = await _live_message(callback)
    if message is None:
        return
    if callback_data.action == ui.TERMS:
        await callback.answer()
        await message.answer(words.terms(get_settings().paysupport_reply_hours))
    elif callback_data.action == ui.CANCEL:
        await callback.answer()
        await message.edit_text(words.CANCELLED)
    elif callback_data.action == ui.ACCEPT:
        await _accept(callback, callback_data, message, bot)
    else:
        await callback.answer(words.STALE_BUTTON, show_alert=True)


async def _accept(
    callback: CallbackQuery, data: ui.BillingCallback, message: Message, bot: Bot
) -> None:
    """«Принимаю условия»: согласие записано, ссылка выдана, кнопка со ссылкой снята."""
    if data.version != words.TERMS_VERSION:
        await callback.answer(words.TERMS_CHANGED, show_alert=True)
        await _show_confirmation(message, bot, callback.from_user.id)
        return
    key = (message.chat.id, message.message_id)
    if not _claim(key):
        await callback.answer(words.ALREADY_ISSUED)
        return
    await callback.answer()
    link = await desks(bot).sales.issue_link(callback.from_user.id)
    if link is None:
        _issued.pop(key, None)
        await message.answer(words.UNAVAILABLE)
        return
    try:
        await message.answer(link.text, reply_markup=ui.link_markup(link.url))
    except TelegramAPIError:
        # Ссылки человек не увидел: кнопка «принимаю» должна остаться рабочей.
        _issued.pop(key, None)
        raise
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError as exc:
        # Не вышло убрать кнопку «принимаю» — не беда, повторное нажатие занято выше.
        log.warning("billing.keyboard_not_removed", error=type(exc).__name__)


# ── платёжные апдейты Telegram ──────────────────────────────────────────────


async def _decide(query: PreCheckoutQuery, bot: Bot) -> Verdict:
    facts = CheckoutFacts(
        buyer_id=query.from_user.id,
        currency=query.currency,
        total_amount=query.total_amount,
        payload=query.invoice_payload,
    )
    return await desks(bot).sales.pre_checkout(facts)


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, bot: Bot) -> None:
    """Последняя точка, где отказ ничего не стоит клиенту: отвечаем всегда и ровно один раз.

    Решение целиком внутри охраны (`guarded_verdict`): сбор сервиса, разбор апдейта, база,
    восьмисекундный бюджет. Ответ уходит один раз, а прерывание поднимается после него.
    """
    verdict = await guarded_verdict(lambda: _decide(query, bot))
    await query.answer(ok=verdict.ok, error_message=verdict.message)
    if verdict.interruption is not None:
        raise verdict.interruption


def _payer_id(message: Message) -> int | None:
    if message.from_user is not None:
        return message.from_user.id
    return message.chat.id if message.chat.type == "private" else None


@router.message(F.successful_payment)
async def paid(message: Message, bot: Bot, event_update: Update) -> None:
    """Деньги сняты: платёж в журнал, ответ клиенту, «не наш» — возврат."""
    payment = message.successful_payment
    if payment is None:  # pragma: no cover — фильтр выше
        return
    facts = PaymentFacts(
        payer_id=_payer_id(message),
        currency=payment.currency,
        total_amount=payment.total_amount,
        payload=payment.invoice_payload,
        charge_id=payment.telegram_payment_charge_id,
        expiration=payment.subscription_expiration_date,
        is_recurring=bool(payment.is_recurring),
        is_first_recurring=bool(payment.is_first_recurring),
        raw=payment.model_dump(mode="json", exclude_none=True),
    )
    reply = await desks(bot).payments.on_payment(facts)
    if reply:
        await message.answer(reply)


@router.message(F.refunded_payment)
async def refunded(message: Message, bot: Bot, event_update: Update) -> None:
    refund = message.refunded_payment
    if refund is None:  # pragma: no cover — фильтр выше
        return
    await desks(bot).payments.on_refunded(
        RefundedFacts(
            payer_id=_payer_id(message),
            charge_id=refund.telegram_payment_charge_id,
            total_amount=refund.total_amount,
            payload=refund.invoice_payload,
            update_id=event_update.update_id,
        )
    )


@router.subscription()
async def subscription_changed(
    change: BotSubscriptionUpdated, bot: Bot, event_update: Update
) -> None:
    await desks(bot).payments.on_subscription(
        update_id=event_update.update_id,
        tg_user_id=change.user.id,
        payload=change.invoice_payload,
        state=change.state,
    )
