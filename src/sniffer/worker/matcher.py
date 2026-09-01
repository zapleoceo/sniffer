"""Новые карточки → подходящие подписки → очередь доставки.

Замыкающее звено контура. До него `listings` копились, а подписки о них не
знали: `matching/` был пустым пакетом, `outbox` никто не наполнял.

Направление обхода — по подпискам, а не по карточкам, и это не всё равно.
Подписок единицы, карточек будут тысячи; обход по карточкам заставлял бы на
каждую спрашивать «а кому это надо», то есть делать запрос на строку. Обход по
подпискам делает один запрос на подписку и берёт сразу пачку — и тот же запрос
служит курсором.

Курсор у каждой подписки свой и хранится в `notifications`: «что уже слали».
Отдельной колонки «докуда дошли» нет намеренно — она разошлась бы с фактом
отправки при любом сбое между двумя записями, а `notifications` и есть факт.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from sniffer.db.engine import session_scope
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.db.repositories.listings import ListingRepository
from sniffer.domain.records import Listing, SubscriptionState
from sniffer.matching import filter_for, score, worth_sending

log = structlog.get_logger(__name__)

# Сколько карточек смотрим на одну подписку за проход. Потолок нужен не для
# скорости: без него первая же подписка на пустой базе прочитала бы весь архив
# одной транзакцией.
LISTINGS_PER_SUBSCRIPTION = 100
SUBSCRIPTIONS_PER_TICK = 50


class Matcher:
    """Один проход сопоставления. Возврат — сколько карточек поставлено в очередь."""

    def __init__(self, *, usd_vnd: float | None = None) -> None:
        # Курс нужен, чтобы долларовый бюджет стал потолком в донгах. Нет курса
        # — потолка нет, и подписка проверяет только смысл: занижать бюджет
        # выдуманным курсом хуже, чем не сужать вовсе.
        self._usd_vnd = usd_vnd

    async def tick(self, *, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        queued = 0
        async with session_scope() as session:
            delivery = DeliveryRepository(session)
            listings = ListingRepository(session)
            for subscription in await delivery.active_subscriptions(limit=SUBSCRIPTIONS_PER_TICK):
                queued += await self._for_subscription(
                    subscription, delivery, listings, moment=moment
                )
            await session.commit()
        return queued

    async def _for_subscription(
        self,
        subscription: SubscriptionState,
        delivery: DeliveryRepository,
        listings: ListingRepository,
        *,
        moment: datetime,
    ) -> int:
        passport = subscription.passport.passport
        spec = filter_for(passport, usd_vnd=self._usd_vnd, now=moment)
        if spec is None:
            # Без города и категории отбор превращается в «покажи всё подряд»,
            # а подписка на всё подряд — это спам, за который бота отключают.
            return 0

        # Сутки считаем от полуночи UTC, а не от полуночи базы: пояс сервера
        # не должен решать, когда у клиента обнуляется лимит.
        midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        left = subscription.max_per_day - await delivery.sent_since(subscription.id, since=midnight)
        if left <= 0:
            return 0

        queued = 0
        for listing in await listings.match(spec, limit=LISTINGS_PER_SUBSCRIPTION):
            if queued >= left:
                break
            if listing.id is None or not worth_sending(listing, passport, now=moment):
                continue
            added = await delivery.enqueue(
                subscription_id=subscription.id,
                user_id=subscription.user_id,
                listing_id=listing.id,
                score=score(listing, passport, now=moment),
                payload=_payload(listing),
                scheduled_at=moment,
            )
            if added:
                queued += 1
        if queued:
            log.info("matcher.queued", subscription=subscription.id, queued=queued)
        return queued


def _payload(listing: Listing) -> dict[str, object]:
    """Что нотифаер покажет клиенту. Карточка собирается при отправке.

    В очередь кладём данные, а не готовый текст: разметка меняется чаще, чем
    ходит очередь, и сообщение, пролежавшее сутки, не должно приезжать в
    прошлогоднем оформлении.
    """
    return {
        "listing_id": listing.id,
        "title": listing.title,
        "summary": listing.summary,
        "url": listing.tg_link,
        "price_amount": str(listing.price_amount) if listing.price_amount is not None else "",
        "price_currency": listing.price_currency or "",
        "posted_at": listing.posted_at.isoformat(),
    }
