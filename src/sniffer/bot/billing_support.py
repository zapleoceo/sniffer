"""Обращения в поддержку: `/paysupport` и `/support` передают вопрос владельцу.

Обязательное условие Telegram к платным ботам (ToS 6.2.1): бот отвечает на `/paysupport`
и обрабатывает обращения по платежам, а поддержка Telegram по покупкам в боте не помогает.
Вопрос приходит в той же команде (`/paysupport оплатил, а подписка не появилась`): так
не нужно состояние «жду текст обращения», которое перехватывало бы следующее сообщение
у поиска. Владельцу уходит текст вместе с последними платежами клиента — по ним он сразу
находит charge id и решает про возврат.
"""

from __future__ import annotations

from datetime import timedelta

from sniffer.bot import billing_owner_wording as owner_words
from sniffer.bot import billing_wording as words
from sniffer.bot.billing_guard import Flow
from sniffer.bot.billing_ports import BotApi, Ledger
from sniffer.domain.billing import BillingEvent, EventKind

# Не больше трёх обращений в час на человека: иначе публичный бот — способ завалить чат
# владельца и упереться в лимиты отправки самого бота.
WINDOW = timedelta(hours=1)
PER_WINDOW = 3
TEXT_LIMIT = 1000
HISTORY = 3


class SupportDesk:
    def __init__(self, *, ledger: Ledger, api: BotApi, owner_id: int, reply_hours: int) -> None:
        self._ledger = ledger
        self._api = api
        self._owner_id = owner_id
        self._reply_hours = reply_hours

    async def handle(
        self, *, command: str, tg_user_id: int, username: str | None, text: str
    ) -> str:
        """Ответ клиенту. Вопрос без текста — подсказка, как его задать."""
        body = text.strip()[:TEXT_LIMIT]
        if not body:
            return self._prompt(command)
        if self._owner_id == 0:
            return words.SUPPORT_UNAVAILABLE
        flow = Flow("support")
        recent = await flow.step(
            "throttle", lambda: self._ledger.events_within(tg_user_id, EventKind.SUPPORT, WINDOW)
        )
        if recent.or_else(0) >= PER_WINDOW:
            flow.finish()
            return words.SUPPORT_THROTTLED
        history = await flow.step(
            "recent_payments", lambda: self._ledger.recent_payments(tg_user_id, HISTORY)
        )
        message = owner_words.owner_support(
            command=command,
            tg_user_id=tg_user_id,
            username=username,
            text=body,
            payments=[owner_words.payment_line(p) for p in history.or_else([])],
        )
        sent = await flow.step("send_owner", lambda: self._api.send_text(self._owner_id, message))
        if sent.ok:
            event = BillingEvent(EventKind.SUPPORT, tg_user_id, {"command": command, "text": body})
            await flow.step("journal", lambda: self._ledger.record_event(event))
        flow.finish()
        return words.support_sent(self._reply_hours) if sent.ok else words.SUPPORT_UNAVAILABLE

    def _prompt(self, command: str) -> str:
        if command == "paysupport":
            return words.paysupport_prompt(self._reply_hours)
        return words.support_prompt(self._reply_hours)
