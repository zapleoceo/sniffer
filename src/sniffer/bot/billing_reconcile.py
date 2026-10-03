"""Сверка журнала платежей с историей звёзд Telegram: расхождения находятся и закрываются.

Платёж и его запись в журнале — два разных события: между снятием звёзд и записью живёт
обработчик апдейта, который может упасть, и процесс, который может перезапуститься. Сверка
ходит каждые четверть часа и закрывает ровно те дыры, что остаются:

* **платёж без записи** — в истории Telegram есть, в журнале нет: записывается тем же
  путём, что и платёж из апдейта (`PaymentDesk.on_payment`), со срочным признаком «срок
  оценён»; «не наш» вернётся сам, клиенту уйдёт ответ;
* **запись без платежа** — в журнале платёж есть, а в истории Telegram его нет: сообщается
  владельцу, ничего не меняется (деньги могли прийти на другой аккаунт бота; решает человек);
* **«не наш» платёж без возврата** — процесс упал между записью платежа и возвратом (дыра
  A5): возврат доводится, а недоведённый `refunding` повторяется;
* **возврат в Telegram, которого нет в журнале** — отмечается, слоты пересчитываются;
* **пересчёт слотов** у всех, у кого за последние дни были платежи: если пересчёт после
  платежа не удался, он удастся здесь.

Режимы (`RECONCILE_MODE`): `off` — сверка не ходит; `report` (по умолчанию) — читает историю
и только сообщает владельцу, что сделала бы; `refund` — записывает недостающее и возвращает.
Платёж со счётом старой модели (`sub:N`) не возвращается автоматически ни в каком режиме.

История листается ДО КОНЦА, а не «первые страницы»: справочник Bot API обещает лишь
«в хронологическом порядке», направление не названо, и сверка не опирается на то, какие
записи свежее.

Каждый шаг охраняется до корня иерархии (`billing_guard.Flow`): сверка не роняет процесс
нотифаера, у которого есть настоящая работа — доставка.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import structlog

from sniffer.bot import billing_owner_wording as owner_words
from sniffer.bot import billing_wording as words
from sniffer.bot.billing import PaymentFacts, is_legacy_payload
from sniffer.bot.billing_guard import Attempt, Flow, describe
from sniffer.bot.billing_payments import PaymentDesk, RefundResult
from sniffer.bot.billing_ports import BotApi, Ledger, Slots
from sniffer.domain import plans
from sniffer.domain.billing import (
    FROM_RECONCILE,
    PAID,
    REFUNDED,
    BillingEvent,
    EventKind,
    PaymentKind,
    StarTransaction,
    StoredPayment,
)
from sniffer.domain.slots import SlotState

log = structlog.get_logger(__name__)

INTERVAL = timedelta(minutes=15)
# 50 страниц по 100 — пять тысяч операций: на порядки больше нашей истории; упереться в
# потолок значит не дойти до конца, и тогда отсутствие платежа ничего не доказывает.
PAGES = 50
PAGE_SIZE = 100
# Платёж моложе этого срока ещё может обрабатываться обработчиком апдейта: сверка его не трогает.
SETTLE_AFTER = timedelta(minutes=10)
LOOKBACK = timedelta(days=2)


class ReconcileMode(StrEnum):
    OFF = "off"
    REPORT = "report"
    REFUND = "refund"


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    recovered: int = 0
    gaps: int = 0
    settled: int = 0
    marked_refunded: int = 0
    resynced: int = 0
    history_failed: bool = False

    @property
    def handled(self) -> int:
        return self.recovered + self.gaps + self.settled + self.marked_refunded


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Every:
    """Запускает работу не чаще раза в интервал; первый вызов — сразу (старт процесса)."""

    def __init__(self, interval: timedelta, clock: Callable[[], float] = time.monotonic) -> None:
        self._interval = interval.total_seconds()
        self._clock = clock
        self._next: float | None = None

    async def run(self, work: Callable[[], Awaitable[int]]) -> int:
        now = self._clock()
        if self._next is not None and now < self._next:
            return 0
        self._next = now + self._interval
        return await work()


class StarsReconciler:
    def __init__(
        self,
        *,
        ledger: Ledger,
        api: BotApi,
        slots: Slots,
        desk: PaymentDesk,
        owner_id: int,
        clock: Callable[[], datetime] = _utcnow,
        mode: ReconcileMode = ReconcileMode.REPORT,
        pages: int = PAGES,
        page_size: int = PAGE_SIZE,
    ) -> None:
        self._ledger = ledger
        self._api = api
        self._slots = slots
        self._desk = desk
        self._owner_id = owner_id
        self._clock = clock
        self._mode = mode
        self._pages = pages
        self._page_size = page_size

    async def tick(self) -> int:
        """Один проход сверки. Возвращает число закрытых расхождений."""
        report = await self.reconcile()
        return report.handled

    async def reconcile(self) -> ReconcileReport:
        if self._mode is ReconcileMode.OFF:
            return ReconcileReport()
        flow = Flow("reconcile")
        now = self._clock()
        history, complete = await self._history(flow)
        if history is None:
            flow.finish()
            return ReconcileReport(history_failed=True)
        recent = (
            await flow.step("recent", lambda: self._ledger.payments_since(now - LOOKBACK))
        ).or_else([])
        recovered = await self._recover(flow, history, now)
        gaps = await self._gaps(flow, history, recent, complete=complete, now=now)
        marked = await self._mark_refunds(flow, history)
        settled = await self._settle(flow, now)
        resynced = await self._resync(flow, recent, now)
        flow.finish()
        return ReconcileReport(recovered, gaps, settled, marked, resynced)

    async def _history(self, flow: Flow) -> tuple[list[StarTransaction] | None, bool]:
        """История звёзд страницами. `complete` — дошли до конца, и отсутствие платежа весомо."""
        found: list[StarTransaction] = []
        for number in range(self._pages):
            offset = number * self._page_size
            page = await self._page(flow, offset)
            if not page.ok:
                return None, False
            got = page.or_else([])
            found.extend(got)
            if len(got) < self._page_size:
                return found, True
        return found, False

    async def _page(self, flow: Flow, offset: int) -> Attempt[list[StarTransaction]]:
        return await flow.step(
            "history", lambda: self._api.star_transactions(offset=offset, limit=self._page_size)
        )

    async def _lookup(self, flow: Flow, charge_id: str) -> Attempt[StoredPayment | None]:
        return await flow.step("get_payment", lambda: self._ledger.get_payment(charge_id))

    async def _mark(self, flow: Flow, charge_id: str) -> Attempt[bool]:
        return await flow.step("mark_refunded", lambda: self._ledger.mark_refunded(charge_id))

    async def _sync(self, flow: Flow, tg_user_id: int, now: datetime) -> Attempt[SlotState]:
        return await flow.step("sync_slots", lambda: self._slots.sync(tg_user_id, now))

    async def _send(self, flow: Flow, chat_id: int, text: str) -> Attempt[None]:
        return await flow.step("tell_client", lambda: self._api.send_text(chat_id, text))

    async def _settle_one(self, flow: Flow, payment: StoredPayment) -> Attempt[RefundResult]:
        return await flow.step("settle_refund", lambda: self._desk.settle_refund(payment))

    # ── платёж без записи ───────────────────────────────────────────────────

    async def _recover(self, flow: Flow, history: list[StarTransaction], now: datetime) -> int:
        refunded = {tx.charge_id for tx in history if not tx.incoming}
        recovered = 0
        for tx in history:
            if not (tx.incoming and tx.invoice_payload is not None and tx.user_id is not None):
                continue
            # Свежий платёж ещё обрабатывает обработчик апдейта: тронуть его значило бы
            # записать второй раз и ответить клиенту дважды.
            if tx.date > now - SETTLE_AFTER:
                continue
            known = await self._lookup(flow, tx.charge_id)
            if not known.ok or known.value is not None:
                continue
            if self._mode is not ReconcileMode.REFUND or is_legacy_payload(tx.invoice_payload):
                await self._report_missing(flow, tx)
                continue
            await self._record_missing(flow, tx, refunded=tx.charge_id in refunded)
            recovered += 1
        return recovered

    async def _report_missing(self, flow: Flow, tx: StarTransaction) -> None:
        user = tx.user_id or 0
        await self._tell_owner_once(
            flow,
            EventKind.RECONCILE_REPORT,
            tx.charge_id,
            user,
            owner_words.owner_report_missing(
                tg_user_id=user, amount=tx.amount, charge_id=tx.charge_id
            ),
        )

    async def _record_missing(self, flow: Flow, tx: StarTransaction, *, refunded: bool) -> None:
        period = timedelta(seconds=tx.subscription_period or plans.SUBSCRIPTION_PERIOD_S)
        facts = PaymentFacts(
            payer_id=tx.user_id,
            currency=plans.SUBSCRIPTION_CURRENCY,
            total_amount=tx.amount,
            payload=tx.invoice_payload or "",
            charge_id=tx.charge_id,
            expiration=int((tx.date + period).timestamp()),
            is_recurring=tx.subscription_period is not None,
            is_first_recurring=False,
            raw={"reconciled": True, "date": tx.date.isoformat()},
            source=FROM_RECONCILE,
            expiration_estimated=True,
        )
        replied = await flow.step("recover_payment", lambda: self._desk.on_payment(facts))
        # Платёж, который в истории уже вернули, записывается (журнал сходится), но
        # «спасибо» за него клиенту не уходит.
        text = None if refunded else replied.or_else(None)
        user = tx.user_id
        if text is not None and user is not None:
            await flow.step("tell_client", lambda: self._api.send_text(user, text))
        await self._tell_owner_once(
            flow,
            EventKind.RECONCILE_GAP,
            tx.charge_id,
            user or 0,
            owner_words.owner_gap_recovered(
                tg_user_id=user or 0,
                amount=tx.amount,
                charge_id=tx.charge_id,
                outcome="Записан сверкой; срок посчитан как дата платежа плюс период.",
            ),
        )

    # ── запись без платежа ──────────────────────────────────────────────────

    async def _gaps(
        self,
        flow: Flow,
        history: list[StarTransaction],
        recent: list[StoredPayment],
        *,
        complete: bool,
        now: datetime,
    ) -> int:
        seen = {tx.charge_id for tx in history if tx.incoming}
        oldest = min((tx.date for tx in history), default=None)
        newest = max((tx.date for tx in history), default=None)
        gaps = 0
        for payment in recent:
            if payment.status == REFUNDED or payment.charge_id in seen:
                continue
            if payment.created_at > now - SETTLE_AFTER:
                continue
            # Неполная история — окно с неизвестного конца: платёж вне окна (старее самой
            # давней или новее самой свежей строки) мог просто не попасть на страницы.
            if not complete and not (
                oldest is not None and newest is not None and oldest <= payment.created_at <= newest
            ):
                continue
            user = payment.tg_user_id or 0
            told = await self._tell_owner_once(
                flow,
                EventKind.RECONCILE_GAP,
                payment.charge_id,
                user,
                owner_words.owner_gap_unpaid(
                    tg_user_id=user, amount=payment.amount, charge_id=payment.charge_id
                ),
            )
            gaps += int(told)
        return gaps

    # ── возвраты ────────────────────────────────────────────────────────────

    async def _mark_refunds(self, flow: Flow, history: list[StarTransaction]) -> int:
        marked = 0
        if self._mode is not ReconcileMode.REFUND:
            return marked
        for tx in history:
            if tx.incoming:
                continue
            stored = await self._lookup(flow, tx.charge_id)
            payment = stored.or_else(None)
            if payment is None or payment.status == REFUNDED:
                continue
            done = await self._mark(flow, tx.charge_id)
            if done.or_else(False) and payment.tg_user_id is not None:
                user = payment.tg_user_id
                await self._sync(flow, user, self._clock())
                marked += 1
        return marked

    async def _settle(self, flow: Flow, now: datetime) -> int:
        """Довести возвраты: «не наши» платежи, которые так и остались `paid`, и `refunding`."""
        stuck = await flow.step(
            "unsettled", lambda: self._ledger.unsettled_refunds(now - SETTLE_AFTER)
        )
        settled = 0
        for payment in stuck.or_else([]):
            if self._mode is not ReconcileMode.REFUND or is_legacy_payload(payment.invoice_payload):
                await self._report_refund(flow, payment)
                continue
            result = await self._settle_one(flow, payment)
            outcome = result.value
            user = payment.tg_user_id
            if outcome is not None and outcome.ok:
                settled += 1
                if user is not None and payment.status == PAID:
                    await self._send(flow, user, words.subscription_refunded())
                continue
            error = (
                describe(result.error)
                if result.error is not None
                else (outcome.error if outcome else "")
            )
            await self._tell_owner_once(
                flow,
                EventKind.REFUND_STUCK,
                payment.charge_id,
                user or 0,
                owner_words.owner_refund_stuck(
                    tg_user_id=user or 0, charge_id=payment.charge_id, error=error
                ),
            )
        return settled

    async def _report_refund(self, flow: Flow, payment: StoredPayment) -> None:
        user = payment.tg_user_id or 0
        await self._tell_owner_once(
            flow,
            EventKind.RECONCILE_REPORT,
            payment.charge_id,
            user,
            owner_words.owner_report_refund(
                tg_user_id=user,
                charge_id=payment.charge_id,
                legacy=is_legacy_payload(payment.invoice_payload),
            ),
        )

    async def _resync(self, flow: Flow, recent: list[StoredPayment], now: datetime) -> int:
        users = sorted(
            {
                p.tg_user_id
                for p in recent
                if p.tg_user_id is not None and p.kind != PaymentKind.UNKNOWN.value
            }
        )
        done = 0
        for user in users:
            synced = await self._sync(flow, user, now)
            done += int(synced.ok)
        return done

    # ── оповещения владельцу ────────────────────────────────────────────────

    async def _tell_owner_once(
        self, flow: Flow, kind: EventKind, charge_id: str, tg_user_id: int, text: str
    ) -> bool:
        """Сказать владельцу об одном расхождении ОДИН раз: повтор каждые 15 минут — шум."""
        known = await flow.step("has_event", lambda: self._ledger.has_event(kind, charge_id))
        if known.or_else(False):
            return False
        if self._owner_id:
            await flow.step("alert_owner", lambda: self._api.send_text(self._owner_id, text))
        else:
            log.error("billing.reconcile_gap", kind=kind.value, charge_id=charge_id)
        event = BillingEvent(kind, tg_user_id, {"stage": kind.value}, charge_id=charge_id)
        await flow.step("record_event", lambda: self._ledger.record_event(event))
        return True
