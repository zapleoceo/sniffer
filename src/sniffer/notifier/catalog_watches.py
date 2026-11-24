"""Deliver new public-catalog matches to the Telegram account that opted in."""

from __future__ import annotations

import asyncio
from html import escape

import structlog

from sniffer.catalog_mcp.watch_tools import MODELS
from sniffer.db.engine import session_scope
from sniffer.db.repositories.catalog_watches import CatalogWatchRepository
from sniffer.domain.districts import PLACE_BY_SLUG
from sniffer.domain.public_catalog import PublicListing
from sniffer.notifier.delivery import Sender
from sniffer.notifier.outcome import Kind, classify
from sniffer.search.market_terms import ALL_CITY_NAMES

log = structlog.get_logger(__name__)
EMPTY_DELAY_S = 90
SUCCESS_DELAY_S = 20
ERROR_DELAY_S = 120
BLOCKED_DELAY_S = 3600


def watch_message(listing: PublicListing) -> str:
    city = ALL_CITY_NAMES[listing.city].get("vi", listing.city)
    location = PLACE_BY_SLUG[listing.district].name if listing.district else city
    facts: list[str] = [escape(location), escape(listing.category.value)]
    if listing.rooms is not None:
        facts.append(f"комнат: {listing.rooms}")
    if listing.price_amount is not None and listing.price_currency is not None:
        price = f"{listing.price_amount:,.0f}".replace(",", " ")
        period = " / мес." if listing.price_period == "month" else ""
        facts.append(f"{price} {listing.price_currency.value}{period}")
    evidence = {
        "marketplace_post": "Прямая ссылка на карточку площадки",
        "telegram_forward_origin": "Исходный пост из данных пересылки Telegram",
        "telegram_observed": "Исходный автор не подтверждён",
    }[listing.source_evidence]
    link = escape(listing.url, quote=True)
    return (
        "🔔 Новое объявление по вашей рассылке\n"
        + " · ".join(facts)
        + (f'\n<a href="{link}">Открыть объявление</a>\n{evidence}')
    )


async def tick(send: Sender) -> bool:
    """Process at most one matching listing per tick; preserve the old owner notifier."""
    async with asyncio.timeout(18), session_scope() as session:
        repo = CatalogWatchRepository(session)
        watch = await repo.take_due()
        if watch is None:
            return False
        listing = await repo.next_listing(watch, cities=ALL_CITY_NAMES, models=MODELS)
        delay = EMPTY_DELAY_S
        if listing is not None:
            # The transaction holds a watch lock and the cross-watch URL reservation.
            # A Telegram error rolls back the reservation, but not the retry delay.
            try:
                async with session.begin_nested():
                    if await repo.reserve_url(watch.tg_chat_id, listing.url):
                        async with asyncio.timeout(7):
                            await send(watch.tg_chat_id, watch_message(listing))
                delay = SUCCESS_DELAY_S
            except Exception as exc:
                failure = classify(exc)
                log.warning(
                    "catalog_watch.delivery_failed", kind=failure.kind, reason=failure.reason
                )
                if failure.kind is Kind.RATE_LIMITED:
                    delay = failure.retry_after
                elif failure.kind is Kind.BLOCKED:
                    delay = BLOCKED_DELAY_S
                else:
                    delay = ERROR_DELAY_S
        await repo.defer(watch, delay=delay)
        await session.commit()
        return True
