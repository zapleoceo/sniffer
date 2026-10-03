"""Что сервису оплаты нужно от внешнего мира: Bot API и журнал платежей.

Протоколы, а не классы: бизнес-логика оплаты не знает ни aiogram, ни SQLAlchemy.
Тот же приём, что у клиента брокера (`broker/contracts.py`): без него сервис нельзя
собрать в тесте без Postgres и без Telegram, а `tests/test_billing_isolation.py`
падает, если модуль с логикой оплаты потянул хоть один модуль базы или aiogram.

Лист по импортам: только `domain`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Protocol

from sniffer.domain.billing import BillingEvent, EventKind, PaymentRecord, StoredPayment


class BotApiError(Exception):
    """Telegram отказал. Текст — описание от Bot API, уже без токена."""


class BotApi(Protocol):
    """Вызовы Bot API, которые нужны оплате. Адаптер — `bot/billing_telegram.py`."""

    async def create_invoice_link(
        self, *, title: str, description: str, payload: str, label: str, amount: int, period_s: int
    ) -> str:
        """Ссылка на подписочный счёт. Подписка выдаётся только ссылкой, не сообщением."""
        ...

    async def refund_star_payment(self, *, user_id: int, charge_id: str) -> None: ...

    async def cancel_star_subscription(self, *, user_id: int, charge_id: str) -> None:
        """Отключить следующее продление; оплаченный период доживает до конца."""
        ...

    async def send_text(self, chat_id: int, text: str) -> None: ...


class Ledger(Protocol):
    """Журнал платежей. Реализация на базе — `bot/billing_ledger.py`.

    Каждый метод — одна короткая единица работы со своей транзакцией: журнал пишется
    первым делом, и держать его открытым вместе с вызовами Telegram нельзя.
    """

    async def record_payment(self, record: PaymentRecord) -> bool:
        """Записать платёж. `False` — этот `charge_id` уже был (повтор апдейта)."""
        ...

    async def get_payment(self, charge_id: str) -> StoredPayment | None: ...

    async def recent_payments(self, tg_user_id: int, limit: int) -> list[StoredPayment]: ...

    async def mark_refunded(self, charge_id: str) -> bool: ...

    async def first_charge_of(self, invoice_payload: str) -> str | None: ...

    async def live_subscriptions(self, tg_user_id: int) -> int: ...

    async def record_consent(self, tg_user_id: int, doc: str, version: str) -> None: ...

    async def has_consent(self, tg_user_id: int, doc: str, version: str) -> bool: ...

    async def record_event(self, event: BillingEvent) -> bool: ...

    async def events_within(self, tg_user_id: int, kind: EventKind, window: timedelta) -> int: ...
