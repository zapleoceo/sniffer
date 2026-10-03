"""Подготовка подписки в живой базе тестов: строка мониторинга с оплаченным сроком.

Прежний `DeliveryRepository.pay_and_activate` удалён вместе со старой моделью «одна подписка
на поиск за 1 звезду»: оплата теперь только пишет платёж в журнал, а слот даёт пересчёт
(`SlotRepository.sync`). Тестам, которым нужна уже включённая подписка, достаточно строки с
нужным сроком — так же в базе выглядит мониторинг, получивший слот.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db import models


async def grant(
    session: AsyncSession,
    user_id: int,
    passport_root: int,
    *,
    until: datetime | None,
    since_listing_id: int = 0,
    priority: int = 0,
) -> int:
    """Завести мониторинг на ветке со сроком `until` (`None` — выдан без платежа)."""
    row = models.Subscription(
        user_id=user_id,
        passport_root=passport_root,
        is_active=True,
        expires_at=until,
        since_listing_id=since_listing_id,
        priority=priority,
    )
    session.add(row)
    await session.flush()
    return row.id
