"""Один слот за один проход: фильтр → суждение → дедуп → потолок → очередь → курсор.

Отдельно от обхода слотов (`monitor.py`): там решают, КОГО брать, и охраняют проход от сбоев,
здесь — что делать с одним слотом. Две причины меняться: порядок обхода и правила отбора
карточек — это разные знания.

Шаги и их порядок (docs/architecture.md 7.2):

1. Фильтр паспорта — тот же построитель, что у диалога (`domain.match_filter`).
2. Вердикт: карточка, которую ИИ-проверка ещё не прочла, ждёт; курсор встаёт ПЕРЕД ней и
   вернётся, когда вердикт появится или выйдет срок (D9).
3. Суждение монитора: возраст при появлении и противоречие паспорту (`matching.worth_sending`).
4. Дедуп с журналом показов: уже показанное клиенту в этом периоде (диалог, другой слот)
   не приходит второй раз и места в потолке не занимает.
5. Потолок слота за вьетнамские сутки: под него берутся НОВЕЙШИЕ, остальное — счёт «ещё N».
6. Очередь + журнал показов (`channel='monitor'`, в потолок периода не входит) + курсор.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.db.repositories.listings import ListingRepository
from sniffer.db.repositories.monitors import MonitorRepository
from sniffer.db.repositories.quota import QuotaRepository
from sniffer.domain.monitoring import (
    OVERFLOW_KIND,
    Overflow,
    add_overflow,
    local_day,
    local_day_start,
    pick,
)
from sniffer.domain.quota import Channel, Claim
from sniffer.domain.records import SubscriptionState
from sniffer.matching import filter_for, score, worth_sending
from sniffer.worker.monitor_queue import LOCAL_ZONE, after_quiet, payload, scheduled

log = structlog.get_logger(__name__)

# Сколько карточек смотрим на слот за проход. Не ради скорости: без потолка первый же слот на
# пустой базе прочитал бы весь архив одной транзакцией.
LISTINGS_PER_SUBSCRIPTION = 100
# Сколько ждём вердикта ИИ-проверки по карточке из архива Telegram, прежде чем доверять без
# него. Проверка обычно укладывается в минуту; пять минут — с запасом на паузу брокера.
VERDICT_WAIT = timedelta(minutes=5)
# Сводка «ещё N» уходит не сразу, а через это время: за полчаса набирается то, что стоит
# назвать числом, и одно сообщение заменяет россыпь.
OVERFLOW_DELAY = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class Stores:
    """Репозитории одного прохода: все на одной сессии, то есть в одной транзакции."""

    monitors: MonitorRepository
    delivery: DeliveryRepository
    listings: ListingRepository
    ledger: QuotaRepository


async def serve_slot(
    subscription: SubscriptionState,
    stores: Stores,
    *,
    moment: datetime,
    usd_vnd: float | None,
    head: int,
) -> int:
    """Обслужить слот. Возврат — сколько карточек поставлено в очередь."""
    passport = subscription.passport.passport
    spec = filter_for(passport, usd_vnd=usd_vnd, now=moment)
    if spec is None:
        # Без города и категории отбор превращается в «покажи всё подряд», а слежение за
        # всем подряд — спам, за который бота отключают.
        return 0
    cursor = max(subscription.since_listing_id, subscription.scan_listing_id)
    if cursor >= head:
        # Новых карточек в базе нет вовсе: запроса за карточками не нужно (D13).
        return 0
    blocked = await stores.listings.first_unready_id(
        spec, after_id=cursor, verdict_before=moment - VERDICT_WAIT
    )
    batch = await stores.listings.match(
        spec, after_id=cursor, before_id=blocked, limit=LISTINGS_PER_SUBSCRIPTION
    )
    ids = [item.id for item in batch if item.id is not None]
    candidates = [item for item in batch if item.id and worth_sending(item, passport, now=moment)]
    shown = await stores.ledger.seen(
        subscription.user_id, [item.id for item in candidates if item.id], moment
    )
    fresh = [item for item in candidates if item.id not in shown]
    used = await stores.delivery.used_since(subscription.id, since=local_day_start(moment))
    chosen = pick(fresh, room=subscription.max_per_day - used)
    queued = []
    for listing in chosen.chosen:
        relevance = score(listing, passport, now=moment)
        # Режим выбирает клиент. Нельзя молча превращать instant в digest из-за внутреннего
        # score: тогда подходящая карточка «пропадает» до вечера, хотя обещана немедленная.
        mode = "digest" if subscription.mode == "digest" else "instant"
        added = await stores.delivery.enqueue(
            subscription_id=subscription.id,
            user_id=subscription.user_id,
            listing_id=listing.id or 0,
            score=relevance,
            payload=payload(listing, delivery_mode=mode),
            scheduled_at=scheduled(subscription, moment, relevance),
            now=moment,
        )
        if added and listing.id is not None:
            queued.append(listing.id)
    if queued:
        await stores.ledger.reserve(
            Claim(
                user_id=subscription.user_id,
                listing_ids=tuple(queued),
                channel=Channel.MONITOR,
                now=moment,
                limit=None,
                passport_root=subscription.passport_root,
            )
        )
    if chosen.overflow:
        await _count_overflow(subscription, stores, chosen.overflow, moment)
    if len(batch) >= LISTINGS_PER_SUBSCRIPTION:
        await stores.delivery.advance_scan(subscription.id, ids[-1])
    elif blocked is not None:
        await stores.delivery.advance_scan(subscription.id, blocked - 1)
    elif ids:
        await stores.delivery.advance_scan(subscription.id, ids[-1])
    if queued:
        log.info("monitor.queued", subscription=subscription.id, queued=len(queued))
    return len(queued)


async def _count_overflow(
    subscription: SubscriptionState, stores: Stores, extra: int, moment: datetime
) -> None:
    """Подошло больше потолка: записать счёт и поставить (или уточнить) сводку «ещё N»."""
    previous = Overflow(
        subscription.overflow_day, subscription.overflow_count, subscription.overflow_notified
    )
    current, notify = add_overflow(previous, today=local_day(moment), extra=extra)
    await stores.monitors.record_overflow(subscription.id, current, extra=extra)
    if notify:
        when = after_quiet((moment + OVERFLOW_DELAY).astimezone(LOCAL_ZONE), subscription)
        await stores.delivery.enqueue_notice(
            subscription_id=subscription.id,
            user_id=subscription.user_id,
            payload={
                "kind": OVERFLOW_KIND,
                "count": current.count,
                "cap": subscription.max_per_day,
            },
            scheduled_at=when.astimezone(UTC),
        )
    else:
        await stores.delivery.bump_overflow_notice(subscription.id, count=current.count)
