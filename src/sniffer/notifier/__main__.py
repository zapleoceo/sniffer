"""Нотифаер: разбирает `outbox` и шлёт сообщения клиентам.

Очередь наполняет `worker/monitor.py`: новая карточка, подошедшая подписке,
становится строкой в `outbox`. Здесь она превращается в сообщение.

Отдельный процесс, а не задача бота: доставка обязана переживать перезапуск
диалога, а темп рассылки нельзя ставить в зависимость от того, занят ли бот
разговором. Токен обязателен — шлём тем же Bot API.
"""

from __future__ import annotations

import asyncio

import structlog
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import LinkPreviewOptions

from sniffer.bot.billing_ledger import DbLedger
from sniffer.bot.billing_payments import PaymentDesk
from sniffer.bot.billing_reconcile import INTERVAL, Every, ReconcileMode, StarsReconciler
from sniffer.bot.billing_slots import DbSlots
from sniffer.bot.billing_telegram import AiogramBotApi
from sniffer.config import Settings, get_settings
from sniffer.notifier.delivery import Delivery, Sender, ThreadSender
from sniffer.notifier.policy import policy_from
from sniffer.runtime.service import Service, idle_loop, run_service
from sniffer.telegram_env import bot_session

log = structlog.get_logger(__name__)

NAME = "notifier"

# Реже воркера: мгновенность здесь не нужна, а троттлинг доставки обязателен —
# бот, шлющий сорок сообщений в день, отключается в первые сутки.
POLL_INTERVAL_S = 10.0


def missing_settings(settings: Settings) -> list[str]:
    return [] if settings.bot_token.strip() else ["BOT_TOKEN"]


async def run(stop: asyncio.Event) -> None:
    log.info("notifier.started")
    settings = get_settings()
    bot = Bot(token=settings.bot_token, session=bot_session(settings))
    delivery = Delivery(
        _sender(bot), send_in_thread=_thread_sender(bot), policy=policy_from(settings)
    )
    reconciler = _reconciler(bot, settings)
    every = Every(INTERVAL)

    async def tick() -> int:
        # Сверка платежей живёт здесь, потому что здесь уже есть Bot и база; ритм — раз в
        # четверть часа и сразу при старте. Её сбой доставку не останавливает: каждый
        # шаг сверки охраняется сам (`billing_guard.Flow`).
        return await delivery.tick() + await every.run(reconciler.tick)

    try:
        await idle_loop(stop, tick, service=NAME, poll_interval_s=POLL_INTERVAL_S)
    finally:
        # Сессия aiohttp живёт внутри Bot: не закрыть её значит оставить
        # предупреждение в логе на каждой остановке контейнера.
        await bot.session.close()


def _reconciler(bot: Bot, settings: Settings) -> StarsReconciler:
    api, ledger, slots = AiogramBotApi(bot), DbLedger(), DbSlots()
    owner = settings.owner_chat_id
    desk = PaymentDesk(
        ledger=ledger,
        api=api,
        slots=slots,
        owner_id=owner,
        reply_hours=settings.paysupport_reply_hours,
    )
    return StarsReconciler(
        ledger=ledger,
        api=api,
        slots=slots,
        desk=desk,
        owner_id=owner,
        mode=ReconcileMode(settings.reconcile_mode),
    )


def _sender(bot: Bot) -> Sender:
    """Отправка через Bot API. Единственное место, где нотифаер знает aiogram."""

    async def send(user_id: int, text: str) -> None:
        await bot.send_message(
            user_id,
            text,
            parse_mode=ParseMode.HTML,
            # Ссылка на объявление в карточке одна и она же — единственное, что
            # клиенту нужно открыть. Предпросмотр рядом с ней только шумит.
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    return send


def _thread_sender(bot: Bot) -> ThreadSender:
    """Отправка в тему личного чата: тот же вызов с `message_thread_id`."""

    async def send(user_id: int, text: str, thread_id: int) -> None:
        await bot.send_message(
            user_id,
            text,
            message_thread_id=thread_id,
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    return send


SERVICE = Service(name=NAME, requires=missing_settings, run=run)

if __name__ == "__main__":
    run_service(SERVICE)
