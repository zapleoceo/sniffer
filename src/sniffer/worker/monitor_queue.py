"""Что и когда ставить в очередь доставки: полезная нагрузка, время, тихие часы.

Выделено из прохода монитора: это знание о доставке (instant, digest, тихие часы по
Вьетнаму), а не о том, как обходятся слоты, и менять их приходится по разным поводам.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sniffer.domain.card_facts import facts_line
from sniffer.domain.quota_period import VIETNAM
from sniffer.domain.records import Listing, SubscriptionState

DIGEST_HOUR = 18
LOCAL_ZONE = VIETNAM


def payload(listing: Listing, *, delivery_mode: str = "instant") -> dict[str, object]:
    """Что нотифаер покажет клиенту. Карточка собирается при отправке.

    В очередь кладём данные, а не готовый текст: разметка меняется чаще, чем
    ходит очередь, и сообщение, пролежавшее сутки, не должно приезжать в
    прошлогоднем оформлении.
    """
    return {
        "listing_id": listing.id,
        "title": listing.title,
        "summary": listing.summary,
        "facts": facts_line(listing.attributes, title=listing.title),
        "url": listing.tg_link,
        "price_amount": str(listing.price_amount) if listing.price_amount is not None else "",
        "price_currency": listing.price_currency or "",
        "posted_at": listing.posted_at.isoformat(),
        "delivery_mode": delivery_mode,
    }


def scheduled(subscription: SubscriptionState, moment: datetime, _relevance: float) -> datetime:
    """Instant, digest и тихие часы в одном детерминированном расчёте."""
    local = moment.astimezone(LOCAL_ZONE)
    if subscription.mode == "digest":
        candidate = local.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
        if candidate <= local:
            candidate += timedelta(days=1)
    else:
        candidate = local
    candidate = after_quiet(candidate, subscription)
    return candidate.astimezone(UTC)


def after_quiet(moment: datetime, subscription: SubscriptionState) -> datetime:
    start, end = subscription.quiet_from, subscription.quiet_to
    if start is None or end is None or start == end:
        return moment
    current = moment.timetz().replace(tzinfo=None)
    overnight = start > end
    inside = current >= start or current < end if overnight else start <= current < end
    if not inside:
        return moment
    target = moment.replace(hour=end.hour, minute=end.minute, second=end.second, microsecond=0)
    if overnight and current >= start:
        target += timedelta(days=1)
    return target
