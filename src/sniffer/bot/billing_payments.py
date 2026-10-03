"""Платёж после списания: записать, ответить, вернуть, отразить возврат и события подписки.

Когда звёзды сняты, любая наша проблема решается на нашей стороне, а не отказом в услуге.
Поэтому здесь платёж сначала ложится в журнал целиком, потом получает ответ, а «не наш»
(не та сумма, не тот счёт, не тот плательщик) возвращается автоматически. Платёж, который
не удалось записать, не пропадает: владельцу уходят все поля, по которым его можно
записать или вернуть вручную.

Каждый шаг охраняется до корня иерархии (`billing_guard.Flow`). Вне `Flow.step` остаётся
только сборка текстов из уже известных полей: наш баг в форматировании прятать не за что,
а охраняемая сборка скрыла бы его за «не удалось».
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from sniffer.bot import billing_owner_wording as owner_words
from sniffer.bot import billing_wording as words
from sniffer.bot.billing import Classification, PaymentFacts, RefundedFacts, classify_payment
from sniffer.bot.billing_guard import Flow, describe
from sniffer.bot.billing_ports import BotApi, BotApiError, Ledger
from sniffer.domain.billing import (
    REFUNDED,
    BillingEvent,
    EventKind,
    PaymentKind,
    PaymentRecord,
    Reason,
)

log = structlog.get_logger(__name__)

ALREADY_REFUNDED = "CHARGE_ALREADY_REFUNDED"


@dataclass(frozen=True, slots=True)
class RefundResult:
    ok: bool
    error: str = ""
    notes: tuple[str, ...] = ()


def already_refunded(error: BaseException | None) -> bool:
    """Повторный возврат того же платежа — успех: цель достигнута, звёзды у клиента."""
    if not isinstance(error, BotApiError):
        return False
    return ALREADY_REFUNDED in re.sub(r"[\s_]+", "_", str(error).upper())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PaymentDesk:
    def __init__(
        self,
        *,
        ledger: Ledger,
        api: BotApi,
        owner_id: int,
        reply_hours: int,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._ledger = ledger
        self._api = api
        self._owner_id = owner_id
        self._reply_hours = reply_hours
        self._clock = clock

    @property
    def enabled(self) -> bool:
        """Есть ли кому отвечать за возвраты: владелец задан."""
        return self._owner_id != 0

    def is_owner(self, tg_user_id: int) -> bool:
        return self.enabled and tg_user_id == self._owner_id

    # ── после списания ──────────────────────────────────────────────────────

    async def on_payment(self, facts: PaymentFacts) -> str | None:
        """Деньги сняты: записать, ответить клиенту, «не наш» платёж вернуть.

        Возвращает текст клиенту; `None` — молчать, это повтор уже обработанного апдейта
        (второе «спасибо» на один платёж выглядит как двойное списание).
        """
        flow = Flow("on_payment")
        unclassified = Classification(PaymentKind.UNKNOWN, Reason.BAD_PAYLOAD)
        done = await flow.compute("classify", lambda: classify_payment(facts, self._clock()))
        verdict = done.or_else(unclassified)
        built = await flow.compute("build_record", lambda: self._record_for(facts, verdict))
        record = built.or_else(None)
        if record is None:
            await self._alert(
                flow,
                owner_words.owner_payment_orphan(
                    amount=facts.total_amount,
                    currency=facts.currency,
                    charge_id=facts.charge_id,
                    payload=facts.payload,
                ),
            )
            flow.finish()
            return words.payment_unrecorded(self._reply_hours)
        entry: PaymentRecord = record
        saved = await flow.step("record", lambda: self._ledger.record_payment(entry))
        if not saved.ok:
            # Журнал недоступен, звёзды сняты: молча терять нельзя. Владелец получает
            # все поля, по которым платёж записывается или возвращается вручную.
            await self._alert(
                flow,
                owner_words.owner_payment_unrecorded(
                    tg_user_id=entry.tg_user_id,
                    amount=facts.total_amount,
                    currency=facts.currency,
                    charge_id=facts.charge_id,
                    payload=facts.payload,
                ),
            )
            flow.finish()
            return words.payment_unrecorded(self._reply_hours)
        if not saved.or_else(False):
            flow.finish()
            return None
        reply = await self._reply_to_payment(flow, entry, facts, verdict)
        flow.finish()
        return reply

    def _record_for(self, facts: PaymentFacts, verdict: Classification) -> PaymentRecord | None:
        """Платёж в виде строки журнала. Плательщик — из апдейта, а нет его — из счёта."""
        payer = facts.payer_id
        if payer is None and verdict.payload is not None:
            payer = verdict.payload.tg_user_id
        if payer is None:
            return None
        return PaymentRecord(
            charge_id=facts.charge_id,
            tg_user_id=payer,
            amount=facts.total_amount,
            currency=facts.currency,
            kind=verdict.kind,
            invoice_payload=facts.payload,
            is_recurring=facts.is_recurring,
            is_first_recurring=facts.is_first_recurring,
            period_end=verdict.period_end,
            raw=facts.raw,
        )

    async def _reply_to_payment(
        self, flow: Flow, entry: PaymentRecord, facts: PaymentFacts, verdict: Classification
    ) -> str:
        if verdict.kind is PaymentKind.UNKNOWN:
            return await self._reject(flow, entry, facts, verdict.reason or Reason.BAD_PAYLOAD)
        until = verdict.period_end or self._clock()
        if verdict.kind is PaymentKind.RENEWAL:
            return words.thanks_renewal(until)
        return words.thanks_first(until)

    async def _reject(
        self, flow: Flow, entry: PaymentRecord, facts: PaymentFacts, reason: Reason
    ) -> str:
        """Платёж не к нашему счёту, не на ту сумму или не от того, кому счёт выписан: вернуть."""
        result = await self._refund(
            flow,
            entry.tg_user_id,
            entry.charge_id,
            entry.invoice_payload,
            recurring=facts.is_recurring or facts.is_first_recurring,
        )
        outcome = "выполнен" if result.ok else f"не удался ({result.error})"
        await self._alert(
            flow,
            owner_words.owner_payment_rejected(
                tg_user_id=entry.tg_user_id,
                reason=reason,
                amount=facts.total_amount,
                currency=facts.currency,
                charge_id=entry.charge_id,
                payload=entry.invoice_payload,
                refund="; ".join([outcome, *result.notes]),
            ),
        )
        return (
            words.payment_refunded(reason)
            if result.ok
            else words.payment_refund_failed(self._reply_hours)
        )

    # ── возврат ─────────────────────────────────────────────────────────────

    async def _refund(
        self, flow: Flow, user_id: int, charge_id: str, payload: str | None, *, recurring: bool
    ) -> RefundResult:
        """Вернуть звёзды и остановить продление. Повторный возврат — успех."""
        called = await flow.step(
            "refund",
            lambda: self._api.refund_star_payment(user_id=user_id, charge_id=charge_id),
        )
        if not called.ok and not already_refunded(called.error):
            return RefundResult(False, describe(called.error or Exception()))
        notes: list[str] = []
        marked = await flow.step("mark_refunded", lambda: self._ledger.mark_refunded(charge_id))
        if not marked.ok:
            notes.append("журнал не обновлён: отметьте возврат вручную")
        if recurring and payload:
            notes += await self._stop_renewal(flow, user_id, payload)
        return RefundResult(True, notes=tuple(notes))

    async def _stop_renewal(self, flow: Flow, user_id: int, payload: str) -> list[str]:
        """Возврат первого платежа подписки без отмены продлил бы её и списал клиента снова."""
        first = await flow.step("first_charge", lambda: self._ledger.first_charge_of(payload))
        charge = first.or_else(None)
        if charge is None:
            return ["первый платёж подписки не найден: отключите продление вручную"]
        stopped = await flow.step(
            "cancel_renewal",
            lambda: self._api.cancel_star_subscription(user_id=user_id, charge_id=charge),
        )
        return [] if stopped.ok else ["продление не отключено: отключите вручную"]

    async def refund(self, charge_id: str, *, user_id: int | None) -> str:
        """Команда владельца `/refund`: вернуть платёж. Возвращает ответ владельцу."""
        flow = Flow("refund")
        found = await flow.step("get_payment", lambda: self._ledger.get_payment(charge_id))
        payment = found.or_else(None)
        target = user_id if user_id is not None else (payment.tg_user_id if payment else None)
        if target is None:
            flow.finish()
            return owner_words.refund_not_in_ledger(charge_id)
        recurring = payment is not None and (payment.is_recurring or payment.is_first_recurring)
        result = await self._refund(
            flow,
            target,
            charge_id,
            payment.invoice_payload if payment else None,
            recurring=recurring,
        )
        if result.ok and not (payment is not None and payment.status == REFUNDED):
            await flow.step(
                "tell_client", lambda: self._api.send_text(target, words.subscription_refunded())
            )
        flow.finish()
        if not result.ok:
            return owner_words.refund_failed(charge_id, result.error)
        return owner_words.refund_done(charge_id, list(result.notes))

    async def on_refunded(self, facts: RefundedFacts) -> None:
        """Сообщение `refunded_payment`: возврат уже случился, надо отразить его в журнале.

        Возврат мог быть сделан не нами (поддержка Telegram, спор): если журнал о нём не
        знал, владелец получает уведомление. Свой возврат журнал уже отметил, и тогда тихо.
        """
        flow = Flow("on_refunded")
        found = await flow.step("get_payment", lambda: self._ledger.get_payment(facts.charge_id))
        marked = await flow.step(
            "mark_refunded", lambda: self._ledger.mark_refunded(facts.charge_id)
        )
        payment = found.or_else(None)
        tg_user_id = payment.tg_user_id if payment is not None else facts.payer_id
        if tg_user_id is not None:
            event = BillingEvent(
                EventKind.REFUNDED,
                tg_user_id,
                {"invoice_payload": facts.payload, "total_amount": facts.total_amount},
                charge_id=facts.charge_id,
                update_id=facts.update_id,
            )
            await flow.step("record_event", lambda: self._ledger.record_event(event))
        if marked.or_else(False) or payment is None:
            await self._alert(
                flow,
                owner_words.owner_refund_outside(
                    tg_user_id=tg_user_id or 0,
                    amount=facts.total_amount,
                    charge_id=facts.charge_id,
                ),
            )
        flow.finish()

    async def _alert(self, flow: Flow, text: str) -> None:
        """Сообщить владельцу. Нет владельца — хотя бы в лог: молча терять нельзя."""
        if not self.enabled:
            log.error("billing.owner_not_configured", alert=text[:300])
            return
        await flow.step("alert_owner", lambda: self._api.send_text(self._owner_id, text))

    # ── события подписки ────────────────────────────────────────────────────

    async def on_subscription(
        self, *, update_id: int | None, tg_user_id: int, payload: str, state: str
    ) -> None:
        """Апдейт `subscription` (Bot API 10.2): отмена, возврат, сбой продления.

        Только журнал. Состояние слота эти события не меняют: доступ считается по срокам
        из платежей, а привязка слотов к мониторингам — отдельная работа. Журнал нужен,
        чтобы отмена и сбой были видны и не терялись: повтор безвреден, дубли отсекает
        `update_id`.
        """
        kinds = {
            "canceled": EventKind.SUB_CANCELED,
            "active": EventKind.SUB_ACTIVE,
            "failed": EventKind.SUB_FAILED,
        }
        event = BillingEvent(
            kinds.get(state, EventKind.SUB_OTHER),
            tg_user_id,
            {"invoice_payload": payload, "state": state},
            update_id=update_id,
        )
        flow = Flow("on_subscription")
        await flow.step("record_event", lambda: self._ledger.record_event(event))
        flow.finish()
