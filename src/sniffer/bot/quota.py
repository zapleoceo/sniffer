"""Квота карточек со стороны бота: допуск, подтверждение, возврат.

Порядок один и для диалога, и для отложенных ответов: сначала `admit` (резерв и
решение, что из найденного положено показать), потом отправка, потом `confirm`
или `release`. Резерв записывается и коммитится ДО отправки: двойное нажатие
запускает два поиска сразу, и схема «показал — потом записал» дала бы обоим
одни и те же карточки. Если отправка не удалась, `release` возвращает слоты; если
процесс умер посередине, резерв снимет чистка по сроку (`QuotaService.sweep`).

Чего здесь нет. SQL: хранилище приходит протоколом `Ledger` (в бою — `SqlLedger`,
в тесте — `MemoryLedger`), поэтому правило проверяется без базы. Подписок:
потолок зависит от числа слотов, а слоты даёт `Entitlements` (по умолчанию их нет
ни у кого; подписки подключает платёжный пакет). Часов: «сейчас» берётся из
внедряемого `Clock`.

Потолок вычисляется В МОМЕНТ выдачи и прошлые выдачи не пересчитывает: подписка
меняет потолок, а не историю. Владелец (`OWNER_CHAT_ID`) и слежение — без потолка,
но журнал пишется и для них: «уже показано» нужно всем.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import structlog

from sniffer.domain.plans import card_cap
from sniffer.domain.quota import (
    OFFER_COOLDOWN,
    Admission,
    Channel,
    Claim,
    Reserved,
    Standing,
    Ticket,
    Usage,
    unique,
)

log = structlog.get_logger(__name__)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Account:
    """Чей это показ: наш id (ключ журнала) и телеграмный (по нему узнают владельца)."""

    user_id: int
    tg_user_id: int


class Entitlements(Protocol):
    """Право тарифа: сколько действующих слотов подписки у аккаунта прямо сейчас."""

    async def slots(self, account: Account, now: datetime) -> int: ...


class NoSubscriptions:
    """Слотов нет ни у кого: все на бесплатном потолке, пока подписки не подключены."""

    async def slots(self, account: Account, now: datetime) -> int:
        return 0


class Ledger(Protocol):
    """Хранилище журнала показов. Каждый вызов — своя транзакция, закоммиченная."""

    async def reserve(self, claim: Claim) -> Reserved: ...

    async def confirm(self, ticket: Ticket, at: datetime) -> None: ...

    async def release(self, ticket: Ticket) -> None: ...

    async def usage(self, user_id: int, now: datetime) -> Usage: ...

    async def claim_offer(self, user_id: int, now: datetime, cooldown: timedelta) -> bool: ...

    async def sweep(self, older_than: datetime, limit: int) -> int: ...


class QuotaService:
    def __init__(
        self,
        ledger: Ledger,
        *,
        entitlements: Entitlements | None = None,
        clock: Clock = utc_now,
        owner_tg_id: int | None = None,
    ) -> None:
        self._ledger = ledger
        self._entitlements: Entitlements = entitlements or NoSubscriptions()
        self._clock = clock
        self._owner_tg_id = owner_tg_id

    async def admit(
        self,
        account: Account,
        listing_ids: Sequence[int],
        channel: Channel = Channel.SEARCH,
        *,
        passport_root: int | None = None,
        request_id: int | None = None,
        now: datetime | None = None,
    ) -> Admission:
        """Что из найденного положено показать этому человеку сейчас.

        Нечего допускать (поиск ничего не нашёл) — пустой `Admission` без записи:
        период начинается с первой РЕАЛЬНО выданной карточки, а не с первого поиска.
        """
        ids = unique(listing_ids)
        if not ids:
            return Admission()
        moment = now or self._clock()
        limit = await self._limit(account, channel, moment)
        reserved = await self._ledger.reserve(
            Claim(
                user_id=account.user_id,
                listing_ids=ids,
                channel=channel,
                now=moment,
                limit=limit,
                passport_root=passport_root,
                request_id=request_id,
            )
        )
        decision = reserved.decision
        return Admission(
            granted=decision.granted,
            repeated=decision.repeated,
            withheld=decision.withheld,
            remaining=decision.remaining,
            limit=limit,
            period_end=reserved.period.end,
            ticket=Ticket(
                user_id=account.user_id,
                period_id=reserved.period_id,
                granted=decision.granted,
                shown=(*decision.granted, *decision.repeated),
                request_id=request_id,
            ),
        )

    async def confirm(self, admission: Admission) -> None:
        """Сообщение дошло: резерв становится показом.

        Сбой подтверждения не роняет ход: человек уже получил карточки, а
        неподтверждённый резерв снимет воркер. Широкий `except` намеренно и с
        записью в лог — так же, как у журнала диалога.
        """
        if admission.ticket is None or not admission.ticket.shown:
            return
        try:
            await self._ledger.confirm(admission.ticket, self._clock())
        except Exception as exc:
            log.warning("quota.confirm_failed", kind=type(exc).__name__, error=str(exc))

    async def release(self, admission: Admission) -> None:
        """Отправка не удалась: вернуть слоты, которые этот допуск занял.

        Не бросает: вызывается из `except`, и ошибка возврата не должна подменять
        настоящую причину. Что не вернулось, снимет воркер по сроку резерва.
        """
        if admission.ticket is None or not admission.ticket.granted:
            return
        try:
            await self._ledger.release(admission.ticket)
        except Exception as exc:
            log.warning("quota.release_failed", kind=type(exc).__name__, error=str(exc))

    async def standing(self, account: Account, *, now: datetime | None = None) -> Standing:
        """Занято, потолок и дата обновления — для `/plan`. Только чтение."""
        moment = now or self._clock()
        usage = await self._ledger.usage(account.user_id, moment)
        limit = await self._limit(account, Channel.SEARCH, moment)
        return Standing(used=usage.used, limit=limit, period_end=usage.period_end)

    async def may_offer(self, account: Account, *, now: datetime | None = None) -> bool:
        """Можно ли сейчас предложить подписку. Право занимается атомарно."""
        return await self._ledger.claim_offer(account.user_id, now or self._clock(), OFFER_COOLDOWN)

    async def sweep(self, older_than: datetime, limit: int) -> int:
        return await self._ledger.sweep(older_than, limit)

    async def _limit(self, account: Account, channel: Channel, now: datetime) -> int | None:
        if self._owner_tg_id is not None and account.tg_user_id == self._owner_tg_id:
            return None
        if not channel.spends_cap:
            return None
        return card_cap(await self._entitlements.slots(account, now))
