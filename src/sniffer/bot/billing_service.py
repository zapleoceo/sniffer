"""Сервис покупки: экран с цифрами, ссылка на счёт и проверка перед списанием.

Оркестрация без знания о Telegram и базе: Bot API и журнал приходят протоколами
(`billing_ports.py`), как приёмник учёта у клиента брокера. Поэтому всё, что здесь
решается про деньги, проверяется тестом с поддельными портами, а не руками в клиенте.
Платёж после списания — `billing_payments.py`, обращения — `billing_support.py`.

**`pre_checkout` отвечает всегда и ровно один раз.** Telegram даёт 10 секунд, иначе
платёж отменяется с общей ошибкой. `guarded_verdict` укладывает решение в 8 секунд и
превращает любой сбой — ошибку базы, зависание, прерывание — в отказ с просьбой
повторить: пока звёзды не сняты, отказ ничего не стоит клиенту.

**Ссылка выдаётся только после согласия с условиями.** Согласие пишется в журнал до
создания ссылки: покупка, в которой нельзя доказать, что клиент прочитал условия, —
нарушение требования Telegram к платным ботам.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from sniffer.bot import billing_wording as words
from sniffer.bot.billing import (
    CheckoutFacts,
    InvoicePayload,
    check_buyer,
    check_price,
    parse_payload,
)
from sniffer.bot.billing_guard import Flow, is_interrupt, let_exit_through
from sniffer.bot.billing_ports import BotApi, Ledger
from sniffer.domain import plans
from sniffer.domain.billing import (
    Reason,
)

log = structlog.get_logger(__name__)

# Telegram даёт на ответ 10 секунд; два — на сам вызов `answerPreCheckoutQuery`.
PRE_CHECKOUT_BUDGET_S = 8.0


@dataclass(frozen=True, slots=True)
class Verdict:
    """Ответ на `pre_checkout_query`: пускать ли платёж дальше."""

    ok: bool
    reason: Reason | None = None
    # Если решение прервали, сам вызов уже получил отказ, а прерывание идёт наверх:
    # проглоченная `CancelledError` ломает остановку процесса.
    interruption: BaseException | None = None

    def __post_init__(self) -> None:
        if not self.ok and self.reason is None:
            raise ValueError("отказ без причины: клиенту нечего сказать")

    @property
    def message(self) -> str | None:
        return None if self.reason is None else words.refusal(self.reason)


async def guarded_verdict(work: Callable[[], Awaitable[Verdict]]) -> Verdict:
    """Вердикт для `pre_checkout`: всегда и вовремя, что бы ни случилось внутри.

    Единственный блок охраны окна: все шаги `BillingService.pre_checkout` (и сборка
    сервиса, и разбор апдейта) выполняются внутри него, а последний `except` — корень
    иерархии. Отказ пока звёзды не сняты ничего не стоит; молчание стоит платежа.
    """
    try:
        return await asyncio.wait_for(work(), timeout=PRE_CHECKOUT_BUDGET_S)
    except BaseException as exc:
        let_exit_through(exc)
        log.error("billing.pre_checkout_failed", error=type(exc).__name__)
        if is_interrupt(exc):
            return Verdict(False, Reason.INTERRUPTED, interruption=exc)
        if isinstance(exc, TimeoutError):
            return Verdict(False, Reason.TIMEOUT)
        return Verdict(False, Reason.UNAVAILABLE)


# Шаги решения. Вызываются по имени модуля, а не по ссылке в таблице: тест подкладывает
# в каждый шаг чужое исключение, и подмена обязана подействовать.


def step_payload(facts: CheckoutFacts) -> InvoicePayload | Reason:
    return parse_payload(facts.payload)


def step_buyer(facts: CheckoutFacts, payload: InvoicePayload) -> Reason | None:
    return check_buyer(facts.buyer_id, payload)


def step_price(facts: CheckoutFacts) -> Reason | None:
    return check_price(facts.currency, facts.total_amount)


async def step_consent(ledger: Ledger, payload: InvoicePayload) -> Reason | None:
    """Согласие с той версией условий, что записана в счёте.

    Версия берётся из счёта, а не текущая: если Telegram присылает `pre_checkout` и на
    автопродление, правка условий не должна срывать продление тем, кто уже согласился.
    """
    agreed = await ledger.has_consent(payload.tg_user_id, words.TERMS_DOC, payload.terms_version)
    return None if agreed else Reason.NO_CONSENT


@dataclass(frozen=True, slots=True)
class Confirmation:
    """Экран «Подписка» с цифрами; `offer=False` — кнопок оплаты не показываем."""

    text: str
    offer: bool


@dataclass(frozen=True, slots=True)
class Link:
    """Выданная ссылка на счёт и подпись к ней."""

    url: str
    text: str


def _new_nonce() -> str:
    return secrets.token_hex(6)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class BillingService:
    def __init__(
        self,
        *,
        ledger: Ledger,
        api: BotApi,
        owner_id: int,
        sales_enabled: bool,
        nonce: Callable[[], str] = _new_nonce,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._ledger = ledger
        self._api = api
        self._owner_id = owner_id
        self._sales_enabled = sales_enabled
        self._nonce = nonce
        self._clock = clock

    @property
    def enabled(self) -> bool:
        """Подписку продаём, когда продажа ВКЛЮЧЕНА и есть кому отвечать за возвраты.

        Флаг `SALES_ENABLED` (по умолчанию выкл) отделён от владельца: владелец в проде
        задан всегда, а продавать можно только когда оплаченный слот работает. Владелец не
        задан — некому вернуть звёзды и получить `/paysupport`, а это обязательное условие
        Telegram к платным ботам. Уже выданные ссылки продолжают работать в обоих случаях
        (платёж никогда не остаётся без разбора).
        """
        return self._sales_enabled and self._owner_id != 0

    # ── покупка ─────────────────────────────────────────────────────────────

    async def confirmation(self, tg_user_id: int) -> Confirmation:
        """Экран с цифрами до ссылки: вторую подписку случайно не покупают."""
        if not self.enabled:
            return Confirmation(words.BILLING_OFF, offer=False)
        flow = Flow("confirmation")
        live = await flow.step(
            "live", lambda: self._ledger.live_subscriptions(tg_user_id, self._clock())
        )
        flow.finish()
        if not live.ok:
            return Confirmation(words.UNAVAILABLE, offer=False)
        return Confirmation(words.confirmation(live.or_else(0)), offer=True)

    async def issue_link(self, tg_user_id: int) -> Link | None:
        """Согласие записано, ссылка создана. `None` — не вышло, ссылки нет."""
        if not self.enabled:
            return None
        flow = Flow("issue_link")
        issued = await flow.step("issue", lambda: self._issue(tg_user_id))
        flow.finish()
        return issued.value if issued.ok else None

    async def _issue(self, tg_user_id: int) -> Link:
        live = await self._ledger.live_subscriptions(tg_user_id, self._clock())
        # Согласие пишется ДО ссылки: ссылка без записанного согласия — это покупка,
        # в которой нельзя доказать, что клиент прочитал условия.
        await self._ledger.record_consent(tg_user_id, words.TERMS_DOC, words.TERMS_VERSION)
        number = live + 1
        payload = InvoicePayload(tg_user_id, words.TERMS_VERSION, self._nonce())
        url = await self._api.create_invoice_link(
            title=words.invoice_title(number),
            description=words.invoice_description(),
            payload=payload.encode(),
            label=words.INVOICE_LABEL,
            amount=plans.SUBSCRIPTION_STARS,
            period_s=plans.SUBSCRIPTION_PERIOD_S,
        )
        return Link(url=url, text=words.link_ready(number))

    # ── до списания ─────────────────────────────────────────────────────────

    async def pre_checkout(self, facts: CheckoutFacts) -> Verdict:
        """Пускать ли платёж: счёт наш, платит его адресат, цена та, условия приняты."""
        payload = step_payload(facts)
        if isinstance(payload, Reason):
            return Verdict(False, payload)
        refusal = (
            step_buyer(facts, payload)
            or step_price(facts)
            or await step_consent(self._ledger, payload)
        )
        return Verdict(True) if refusal is None else Verdict(False, refusal)
