"""Дубль объявления на входе: что берётся от первоисточника, а что — от свежей копии.

Одно объявление — одна карточка, и её ссылка ведёт на **первоисточник**: самый
ранний пост, который клиент может открыть (`t.me/c/…` открывается только
участникам чата). От самой свежей копии карточка берёт **цену** и **свежесть**
(`posted_at`), иначе она погасла бы по возрасту раньше живого перепоста.

До 16.11.2026 было иначе: свежий перепост забирал карточку целиком, вместе со
ссылкой, — и клиент получал ссылку на десятую копию вместо оригинала. Тот же
принцип применяет уборка базы (`domain/listing_dedup.py`), так что вход и
уборка не спорят друг с другом.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sniffer.domain.listing_dedup import PRICE_ATTRIBUTES
from sniffer.domain.records import Listing

PRICE_COLUMNS = ("price_amount", "price_currency", "price_period", "price_usd_month")
# Поля, привязанные к конкретному посту: переезжают только вместе со ссылкой.
POST_COLUMNS = (
    "raw_message_id",
    "source",
    "external_id",
    "seller_id",
    "title",
    "summary",
    "district",
    "tg_link",
    "lang",
    "confidence",
)


def primary_rank(listing: Listing) -> tuple[bool, datetime]:
    """Меньше — ближе к первоисточнику: публичная ссылка, затем более ранний пост."""
    return (listing.tg_link.startswith("https://t.me/c/"), listing.posted_at)


def merge_values(existing: Listing, candidate: Listing) -> dict[str, Any]:
    """Правка существующей карточки новой копией того же объявления. Пусто — менять нечего."""
    owner = candidate if primary_rank(candidate) < primary_rank(existing) else existing
    newer = candidate if candidate.posted_at > existing.posted_at else existing
    priced = newer if _has_price(newer) else (existing if _has_price(existing) else candidate)
    wanted: dict[str, Any] = {column: getattr(owner, column) for column in POST_COLUMNS}
    wanted.update({column: getattr(priced, column) for column in PRICE_COLUMNS})
    wanted["attributes"] = {
        **{k: v for k, v in owner.attributes.items() if k not in PRICE_ATTRIBUTES},
        **{k: v for k, v in priced.attributes.items() if k in PRICE_ATTRIBUTES},
    }
    wanted["posted_at"] = max(existing.posted_at, candidate.posted_at)
    values = {key: value for key, value in wanted.items() if getattr(existing, key) != value}
    if "summary" in values:
        # Текст сменился — прежний вердикт ИИ-проверки читал другой текст.
        values["screened_at"] = None
    return values


def _has_price(listing: Listing) -> bool:
    return listing.price_amount is not None or any(
        k in PRICE_ATTRIBUTES for k in listing.attributes
    )
