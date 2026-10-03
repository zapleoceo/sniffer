"""Квота в бою получает право тарифа из журнала платежей, а не «слотов нет ни у кого».

Без этой проводки оплативший оставался бы на бесплатных 10 карточках: `QuotaService` по
умолчанию ставит `NoSubscriptions`, и тесты сервиса с подставным правом этого не видят.
"""

from __future__ import annotations

from sniffer.bot.billing_slots import LedgerEntitlements
from sniffer.bot.quota import NoSubscriptions
from sniffer.bot.quota_ledger import new_quota


def test_the_production_quota_reads_slots_from_the_payment_ledger() -> None:
    quota = new_quota()

    entitlements = quota._entitlements
    assert isinstance(entitlements, LedgerEntitlements)
    assert not isinstance(entitlements, NoSubscriptions)
