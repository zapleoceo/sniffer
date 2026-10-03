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

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog

from sniffer.db.engine import session_scope
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.db.repositories.listings import ListingRepository
from sniffer.domain.records import Listing, SubscriptionState
from sniffer.matching import filter_for, needs_usd_rate, score, worth_sending
from sniffer.worker.usd_rate import RateSource, UsdRate

log = structlog.get_logger(__name__)

# Сколько карточек смотрим на одну подписку за проход. Потолок нужен не для
# скорости: без него первая же подписка на пустой базе прочитала бы весь архив
# одной транзакцией.
LISTINGS_PER_SUBSCRIPTION = 100
SUBSCRIPTIONS_PER_TICK = 50
DIGEST_HOUR = 18
LOCAL_ZONE = ZoneInfo("Asia/Ho_Chi_Minh")

# Порт часов. Проход живёт ОДНИМ моментом: срок подписки, окно карточек, граница суток
# и время постановки в очередь считаются от него, а не от часов, которые каждый вызов
# снимает сам. Иначе проход «на заданном времени» (тест, догон после простоя)
# судил бы подписки по одному «сейчас», а карточки — по другому.
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class MatcherCounters:
    """Что проход сделал НЕ по плану, накопительно с запуска процесса.

    Число, а не только строка в журнале: «сколько подписок ждёт курса» и «сколько в
    карантине» — вопросы для панели и сторожа, а искать их grep-ом по логу нельзя.
    """

    skipped_no_rate: int = 0


class Matcher:
    """Один проход сопоставления. Возврат — сколько карточек поставлено в очередь."""

    def __init__(self, *, rate: RateSource | None = None, clock: Clock = _utc_now) -> None:
        # Курс нужен, чтобы долларовый бюджет стал потолком в донгах. Источник не задан
        # или не ответил — курса нет, и такая подписка ждёт (см. `tick`). Выдуманный курс
        # занизил бы бюджет, а отбор без курса не сузил бы его вовсе: подписка шлёт сама,
        # и дорогое объявление ушло бы клиенту как «идеально в бюджете».
        self._rate = UsdRate(rate)
        self._clock = clock
        self.counters = MatcherCounters()

    async def tick(self, *, now: datetime | None = None) -> int:
        moment = now or self._clock()
        queued = 0
        async with session_scope() as session:
            delivery = DeliveryRepository(session)
            listings = ListingRepository(session)
            due = await delivery.active_subscriptions(limit=SUBSCRIPTIONS_PER_TICK, now=moment)
            waiting: list[int] = []
            for subscription in due:
                usd_vnd: float | None = None
                if needs_usd_rate(subscription.passport.passport):
                    usd_vnd = await self._rate.get(moment)
                    if usd_vnd is None:
                        # Ни слать без бюджета, ни двигать курсор: когда курс вернётся,
                        # подписка возьмёт всё с того же места, ничего не потеряв.
                        waiting.append(subscription.id)
                        continue
                queued += await self._for_subscription(
                    subscription, delivery, listings, moment=moment, usd_vnd=usd_vnd
                )
            if waiting:
                self.counters.skipped_no_rate += len(waiting)
                log.warning("matcher.waiting_for_rate", subscriptions=waiting)
            await session.commit()
        return queued

    async def _for_subscription(
        self,
        subscription: SubscriptionState,
        delivery: DeliveryRepository,
        listings: ListingRepository,
        *,
        moment: datetime,
        usd_vnd: float | None,
    ) -> int:
        passport = subscription.passport.passport
        spec = filter_for(passport, usd_vnd=usd_vnd, now=moment)
        if spec is None:
            # Без города и категории отбор превращается в «покажи всё подряд»,
            # а подписка на всё подряд — это спам, за который бота отключают.
            return 0

        # Сутки считаем от полуночи UTC, а не от полуночи базы: пояс сервера
        # не должен решать, когда у клиента обнуляется лимит.
        midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        left = subscription.max_per_day - await delivery.used_since(subscription.id, since=midnight)
        if left <= 0:
            return 0

        queued = 0
        # Точка отсчёта подписки. Без неё свежая подписка вываливает клиенту
        # весь двухнедельный запас разом — включая ровно те объявления, которые
        # он посмотрел и не выбрал перед тем, как заплатить. Подписка обещает
        # НОВЫЕ посты, и обещание держится этим аргументом.
        batch = await listings.match(
            spec,
            after_id=max(subscription.since_listing_id, subscription.scan_listing_id),
            limit=LISTINGS_PER_SUBSCRIPTION,
        )
        last_examined = 0
        for listing in batch:
            if queued >= left:
                break
            if listing.id is not None:
                last_examined = listing.id
            if listing.id is None or not worth_sending(listing, passport, now=moment):
                continue
            relevance = score(listing, passport, now=moment)
            # Режим выбирает клиент. Нельзя молча превращать instant в digest
            # из-за внутреннего score: тогда подходящая карточка «пропадает»
            # до вечера, хотя подписка обещала немедленную доставку.
            delivery_mode = "digest" if subscription.mode == "digest" else "instant"
            added = await delivery.enqueue(
                subscription_id=subscription.id,
                user_id=subscription.user_id,
                listing_id=listing.id,
                score=relevance,
                payload=_payload(listing, delivery_mode=delivery_mode),
                scheduled_at=_scheduled(subscription, moment, relevance),
                now=moment,
            )
            if added:
                queued += 1
        if last_examined:
            await delivery.advance_scan(subscription.id, last_examined)
        if queued:
            log.info("matcher.queued", subscription=subscription.id, queued=queued)
        return queued


def _payload(listing: Listing, *, delivery_mode: str = "instant") -> dict[str, object]:
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
        "delivery_mode": delivery_mode,
    }


def _scheduled(subscription: SubscriptionState, moment: datetime, _relevance: float) -> datetime:
    """Instant, digest и тихие часы в одном детерминированном расчёте."""
    local = moment.astimezone(LOCAL_ZONE)
    if subscription.mode == "digest":
        candidate = local.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
        if candidate <= local:
            candidate += timedelta(days=1)
    else:
        candidate = local
    candidate = _after_quiet(candidate, subscription)
    return candidate.astimezone(UTC)


def _after_quiet(moment: datetime, subscription: SubscriptionState) -> datetime:
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
