"""Агент слежения: обход слотов, право, курс, изоляция сбоев. Логический агент, не процесс.

Слот = одна подписка = один фильтр (текущая версия цепочки паспорта). Агент — задача asyncio
в воркере со своим циклом и своим предохранителем (`worker/__main__.py`): ИИ-проверка и
остальная воронка у него не стоят в цепочке `await`, и сбой одного не останавливает другого.
Что делать с одним слотом — `monitor_scope.serve_slot`; здесь решается, КОГО брать и можно ли.

Курсор и дедуп у каждого слота свои: `since_listing_id` — точка начала, `scan_listing_id` —
докуда карточки просмотрены, `notifications` — дедуп постановки в очередь. Правка фильтра
клиентом — новая версия паспорта той же цепочки: слот читает ТЕКУЩУЮ версию, курсор и история
не трогаются, и правка подхватывается на ближайшем проходе.

Свойства прохода:

* **обход по кругу** — порция из `monitor_batch` слотов, кого не смотрели дольше всех первым;
* **слот в своём SAVEPOINT** — сбой одного откатывает только его и отправляет в карантин;
* **часы и курс — зависимости**: проход живёт одним моментом, а слот с долларовым бюджетом без
  курса ждёт, а не шлёт без фильтра бюджета;
* **право на доставку** — первым шагом отменяется очередь слотов, истёкших раньше льготы;
  новое ставится только слоту, у которого право есть в этот момент;
* **пауза без слота (no_slot)** — слоты сверх числа подписок Stars не сканируются, но привязка,
  курсор и история целы; после долгой паузы курсор возвращается к «сейчас».
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.config import get_settings
from sniffer.db.engine import session_scope
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.db.repositories.listings import ListingRepository
from sniffer.db.repositories.monitors import (
    BrokenSubscription,
    MonitorRepository,
    describe_error,
)
from sniffer.db.repositories.quota import QuotaRepository
from sniffer.domain.monitoring import must_jump_to_head, open_slot_ids
from sniffer.domain.records import SubscriptionState
from sniffer.matching import needs_usd_rate
from sniffer.worker.monitor_scope import Stores, serve_slot
from sniffer.worker.quarantine import quarantine
from sniffer.worker.usd_rate import RateSource, UsdRate

log = structlog.get_logger(__name__)

# Порт часов. Проход живёт ОДНИМ моментом: срок подписки, окно карточек, граница суток
# и время постановки в очередь считаются от него, а не от часов, которые каждый вызов
# снимает сам. Иначе проход «на заданном времени» (тест, догон после простоя)
# судил бы подписки по одному «сейчас», а карточки — по другому.
Clock = Callable[[], datetime]


class Slots(Protocol):
    """Сколько слотов слежения у клиента сейчас. `None` — не знаем, ограничения нет."""

    async def count(self, user_id: int, now: datetime) -> int | None: ...


class UnlimitedSlots:
    """Без ограничения числа слотов: все слоты с правом работают.

    Умолчание для сборки без биллинга (тесты). В бою `build_monitor` подключает
    `worker.slot_ledger.LedgerSlots`: число слотов — живые подписки Stars клиента.
    """

    async def count(self, user_id: int, now: datetime) -> int | None:
        return None


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class MonitorCounters:
    """Что проход сделал НЕ по плану, накопительно с запуска процесса.

    Число, а не только строка в журнале: «сколько слотов ждёт курса», «стоит без слота» и «в
    карантине» — вопросы для панели и сторожа, а искать их grep-ом по логу нельзя.
    """

    skipped_no_rate: int = 0
    quarantined: int = 0
    cancelled_lapsed: int = 0
    paused_no_slot: int = 0


class MonitorAgent:
    """Один проход обхода слотов. Возврат — сколько карточек поставлено в очередь."""

    def __init__(
        self,
        *,
        rate: RateSource | None = None,
        slots: Slots | None = None,
        clock: Clock = _utc_now,
        batch: int | None = None,
        lapse_grace: timedelta | None = None,
    ) -> None:
        # Курс нужен, чтобы долларовый бюджет стал потолком в донгах. Источник не задан
        # или не ответил — курса нет, и такой слот ждёт (см. `tick`). Выдуманный курс
        # занизил бы бюджет, а отбор без курса не сузил бы его вовсе: слот шлёт сам,
        # и дорогое объявление ушло бы клиенту как «идеально в бюджете».
        self._rate = UsdRate(rate)
        self._slots: Slots = slots or UnlimitedSlots()
        self._clock = clock
        settings = get_settings()
        self._batch = settings.monitor_batch if batch is None else batch
        self._grace = (
            timedelta(hours=settings.monitor_lapse_grace_hours)
            if lapse_grace is None
            else lapse_grace
        )
        self.counters = MonitorCounters()

    async def tick(self, *, now: datetime | None = None) -> int:
        moment = now or self._clock()
        queued = 0
        async with session_scope() as session:
            stores = Stores(
                MonitorRepository(session),
                DeliveryRepository(session),
                ListingRepository(session),
                QuotaRepository(session),
            )
            # Первым делом — отмена того, что стоит в очереди после льготы: проход, который
            # ставит новое, не вправе оставлять за собой просроченное старое (D7).
            lapsed = await stores.monitors.cancel_lapsed(now=moment, grace=self._grace)
            if lapsed:
                self.counters.cancelled_lapsed += lapsed
                log.info("monitor.lapsed_cancelled", cancelled=lapsed)
            # Слот без права порция уже не выбирает: начало его паузы записываем здесь, до
            # выбора, иначе вернувшийся слот не узнает, сколько простоял.
            paused = await stores.monitors.mark_lapsed(now=moment)
            if paused:
                log.info("monitor.paused_without_right", slots=paused)
            due = await stores.monitors.claim_due(limit=self._batch, now=moment)
            for sick in due.broken:
                # Паспорт не разобрался: до слота дело не дошло, но молчать о нём нельзя.
                await self._quarantine(stores.monitors, sick, sick.error, moment)
            working = await self._working(stores.monitors, due.ready, moment)
            head = await stores.listings.max_id() if working else 0
            waiting: list[int] = []
            for subscription in due.ready:
                if subscription.id not in working:
                    await self._pause(stores, subscription, moment)
                    continue
                usd_vnd: float | None = None
                if needs_usd_rate(subscription.passport.passport):
                    usd_vnd = await self._rate.get(moment)
                    if usd_vnd is None:
                        # Ни слать без бюджета, ни двигать курсор: когда курс вернётся,
                        # слот возьмёт всё с того же места, ничего не потеряв. Но обойдённым
                        # он считается — иначе ждущие стояли бы первыми и заморили остальных.
                        waiting.append(subscription.id)
                        await stores.monitors.touch(subscription.id, now=moment)
                        continue
                queued += await self._isolated(
                    session, stores, subscription, moment=moment, usd_vnd=usd_vnd, head=head
                )
            if waiting:
                self.counters.skipped_no_rate += len(waiting)
                log.warning("monitor.waiting_for_rate", subscriptions=waiting)
            await session.commit()
        return queued

    async def _working(
        self, monitors: MonitorRepository, ready: list[SubscriptionState], moment: datetime
    ) -> set[int]:
        """Какие из порции вправе работать: первые `slots` слотов клиента, остальные стоят."""
        users = sorted({subscription.user_id for subscription in ready})
        if not users:
            return set()
        ranked = await monitors.ranked_slots(users, now=moment)
        # Слот без срока (`expires_at IS NULL`: выдан владельцем, без платежа) в число
        # оплаченных не входит и от него не зависит: считать его в ранг значило бы
        # остановить все такие слоты, у клиента-то оплаченных ноль.
        unmetered = {item.id for item in ready if item.expires_at is None}
        working: set[int] = set(unmetered)
        for user_id in users:
            granted = await self._slots.count(user_id, moment)
            paid = [item for item in ranked.get(user_id, []) if item not in unmetered]
            working |= open_slot_ids(paid, granted)
        return working

    async def _pause(
        self, stores: Stores, subscription: SubscriptionState, moment: datetime
    ) -> None:
        """Слот без права: не сканируем, но помним, с какого момента, и считаем обойдённым."""
        if subscription.no_slot_since is None:
            await stores.monitors.set_no_slot_since(subscription.id, moment)
        await stores.monitors.touch(subscription.id, now=moment)
        self.counters.paused_no_slot += 1

    async def _isolated(
        self,
        session: AsyncSession,
        stores: Stores,
        subscription: SubscriptionState,
        *,
        moment: datetime,
        usd_vnd: float | None,
        head: int,
    ) -> int:
        """Слот в своём SAVEPOINT: сбой откатывает только его и уводит его в карантин.

        Охраняем до `Exception`, а не до `BaseException`, и это не недосмотр: остановка
        процесса (`CancelledError` при SIGTERM, `KeyboardInterrupt`) — не сбой слота, и
        отправлять его за это в карантин нельзя. Типы ожидаемых ошибок не перечисляем:
        список ожиданий и есть дефект, больной слот роняет проход чем угодно.
        """
        try:
            async with session.begin_nested():
                if subscription.no_slot_since is not None:
                    subscription = await self._resume(stores, subscription, moment, head)
                queued = await serve_slot(
                    subscription, stores, moment=moment, usd_vnd=usd_vnd, head=head
                )
                await stores.monitors.record_scan(subscription.id, now=moment)
        except Exception as exc:
            await self._quarantine(
                stores.monitors, subscription, describe_error(exc), moment, cause=exc
            )
            return 0
        return queued

    async def _resume(
        self, stores: Stores, subscription: SubscriptionState, moment: datetime, head: int
    ) -> SubscriptionState:
        """Слот вернул право: после долгой паузы курсор к «сейчас», короткая его не двигает.

        Курсор сдвинут в БД, а `subscription` в памяти хранит старый: слот сразу после этого
        обслуживается по нему же и увидел бы старое. Поэтому обслуживание получает копию с
        уже сдвинутым курсором (`replace`).
        """
        jump = must_jump_to_head(subscription.no_slot_since, moment)
        if jump:
            await stores.delivery.advance_scan(subscription.id, head)
        await stores.monitors.set_no_slot_since(subscription.id, None)
        log.info("monitor.resumed", subscription=subscription.id, jumped=jump)
        cursor = max(subscription.scan_listing_id, head) if jump else subscription.scan_listing_id
        return replace(subscription, scan_listing_id=cursor, no_slot_since=None)

    async def _quarantine(
        self,
        monitors: MonitorRepository,
        slot: SubscriptionState | BrokenSubscription,
        error: str,
        moment: datetime,
        cause: BaseException | None = None,
    ) -> None:
        await quarantine(monitors, slot, error=error, moment=moment, cause=cause)
        self.counters.quarantined += 1
