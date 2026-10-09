"""Кандидат слежения, который уходит в комнату агентов. Модель без ввода-вывода."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class RelayCandidate:
    notification_id: int
    subscription_id: int
    score: float
    tg_link: str
    posted_at: datetime
    price_amount: Decimal | None
    price_currency: str | None
    price_period: str | None
    district: str | None
    city: str
    title: str
    summary: str
    attributes: dict[str, Any]
    # Исходный текст поста; None, если сырой записи уже нет (чистка архива).
    text: str | None
    has_media: bool
    media_count: int
