"""Журнал показанных карточек: весь SQL квоты.

Алгоритм одного резерва — одна транзакция, которую вызывающий коммитит ДО
отправки сообщения (репозиторий сам не коммитит, как и остальные):

1. Якорь аккаунта. Пуст — ставится ровно один раз условным `UPDATE ... WHERE
   quota_anchor_at IS NULL`; проигравший гонку перечитывает чужой.
2. Период: номер и границы считает домен (`numbered_period`), строка создаётся
   лениво под `ON CONFLICT (user_id, period_no) DO NOTHING`. Если домен ошибётся в
   арифметике, вставку отклонит CHECK таблицы — граница не запишется «не та».
3. Блокировка строки периода (`SELECT ... FOR UPDATE`). Параллельные ответы одного
   клиента встают в очередь: при READ COMMITTED второй после ожидания читает уже
   зафиксированное число занятых, поэтому «осталось 3» не видят оба.
4. Под блокировкой: что из запрошенного уже есть в периоде, сколько занято, решение
   (`domain.quota.decide`), вставка новых и учёт повторов.

Триггера со счётчиком нет намеренно: цепочка миграций не правит данные
(`tests/test_sql_chain.py`), а число занятых — это `count(*)` под той же блокировкой.
Последний барьер — уникальность `(period_id, listing_id)`: она не даст записать
карточку дважды, даже если вызывающий забудет блокировку.

«Сейчас» всегда приходит параметром. `now()` базы здесь не используется: тест не
смог бы подставить момент, а граница периода — это момент, а не показание часов.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast

from sqlalchemy import Table, delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sniffer.db import models
from sniffer.db.repositories.base import Repository
from sniffer.domain.quota import (
    Channel,
    Claim,
    Reserved,
    Ticket,
    Usage,
    decide,
    numbered_period,
    unique,
)
from sniffer.domain.quota_period import Period

# Резервы, которые сам воркер вправе снимать. У слежения подтверждение придёт от
# нотификатора, возможно через часы (тихие часы, дайджест), — снять его по таймеру
# значило бы потерять запись о том, что карточка присылалась.
SWEPT_CHANNELS = (Channel.SEARCH.value, Channel.DEFERRED.value)


def _table(model: type[models.Base]) -> Table:
    return cast(Table, model.__table__)


class QuotaRepository(Repository):
    async def reserve(self, claim: Claim) -> Reserved:
        """Записать карточки, которые можно показать, и вернуть решение."""
        ids = unique(claim.listing_ids)
        if not ids:
            raise ValueError("нечего резервировать: список карточек пуст")
        anchor = await self._anchor(claim.user_id, claim.now)
        number, period = numbered_period(anchor, claim.now)
        period_id = await self._lock_period(claim.user_id, anchor, number, period)

        seen = set(
            await self._session.scalars(
                select(models.OfferView.listing_id).where(
                    models.OfferView.period_id == period_id,
                    models.OfferView.listing_id.in_(ids),
                )
            )
        )
        used = int(
            await self._session.scalar(
                select(func.count())
                .select_from(models.OfferView)
                .where(
                    models.OfferView.period_id == period_id,
                    models.OfferView.channel != Channel.MONITOR.value,
                )
            )
            or 0
        )
        decision = decide(ids, seen=seen, used=used, limit=claim.limit)
        await self._insert(claim, period_id, decision.granted)
        if decision.repeated:
            await self._session.execute(
                update(_table(models.OfferView))
                .where(
                    models.OfferView.period_id == period_id,
                    models.OfferView.listing_id.in_(decision.repeated),
                )
                .values(times_shown=models.OfferView.times_shown + 1, last_shown_at=claim.now)
            )
        if decision.withheld and claim.request_id is not None:
            await self._session.execute(
                update(_table(models.ClientRequest))
                .where(
                    models.ClientRequest.id == claim.request_id,
                    models.ClientRequest.user_id == claim.user_id,
                )
                .values(withheld_count=models.ClientRequest.withheld_count + len(decision.withheld))
            )
        return Reserved(period_id=period_id, period=period, decision=decision)

    async def _anchor(self, user_id: int, now: datetime) -> datetime:
        row = (
            await self._session.execute(
                select(models.User.id, models.User.quota_anchor_at).where(models.User.id == user_id)
            )
        ).one_or_none()
        if row is None:
            raise LookupError(f"клиент {user_id} не найден")
        existing: datetime | None = row[1]
        if existing is not None:
            return existing
        users = _table(models.User)
        won = await self._session.scalar(
            update(users)
            .where(users.c.id == user_id, users.c.quota_anchor_at.is_(None))
            .values(quota_anchor_at=now)
            .returning(users.c.quota_anchor_at)
        )
        if won is not None:
            return cast(datetime, won)
        # Проиграли гонку: условный UPDATE дождался чужого коммита и ничего не
        # тронул. Якорь теперь чужой, и он единственный верный.
        theirs = await self._session.scalar(
            select(models.User.quota_anchor_at).where(models.User.id == user_id)
        )
        if theirs is None:  # pragma: no cover — якорь не снимают
            raise RuntimeError(f"у клиента {user_id} пропал якорь квоты")
        return theirs

    async def _lock_period(
        self, user_id: int, anchor: datetime, number: int, period: Period
    ) -> int:
        await self._session.execute(
            pg_insert(_table(models.QuotaPeriod))
            .values(
                user_id=user_id,
                anchor_at=anchor,
                period_no=number,
                period_start=period.start,
                period_end=period.end,
            )
            .on_conflict_do_nothing(index_elements=["user_id", "period_no"])
        )
        locked = await self._session.scalar(
            select(models.QuotaPeriod.id)
            .where(
                models.QuotaPeriod.user_id == user_id,
                models.QuotaPeriod.period_no == number,
            )
            .with_for_update()
        )
        if locked is None:  # pragma: no cover — строка только что вставлена или уже была
            raise RuntimeError(f"период {number} клиента {user_id} не читается")
        return int(locked)

    async def _insert(self, claim: Claim, period_id: int, granted: tuple[int, ...]) -> None:
        if not granted:
            return
        table = _table(models.OfferView)
        rows = [
            {
                "user_id": claim.user_id,
                "period_id": period_id,
                "listing_id": listing_id,
                "passport_root": claim.passport_root,
                "request_id": claim.request_id,
                "channel": claim.channel.value,
                "shown_at": claim.now,
                "last_shown_at": claim.now,
            }
            for listing_id in granted
        ]
        inserted = set(
            await self._session.scalars(
                pg_insert(table)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["period_id", "listing_id"])
                .returning(table.c.listing_id)
            )
        )
        if inserted != set(granted):
            # Под блокировкой периода такого быть не может: «виденное» уже отделено.
            # Тихо пропустить конфликт — значит показать карточку, которой в журнале
            # нет или которая записана другим ответом, поэтому падаем громко.
            raise RuntimeError(
                f"журнал показов: карточки {sorted(set(granted) - inserted)} уже записаны "
                "в период — блокировка периода не удержана"
            )

    async def confirm(self, ticket: Ticket, *, at: datetime) -> None:
        """Telegram принял сообщение: резерв становится показом."""
        if not ticket.shown:
            return
        await self._session.execute(
            update(_table(models.OfferView))
            .where(
                models.OfferView.period_id == ticket.period_id,
                models.OfferView.user_id == ticket.user_id,
                models.OfferView.listing_id.in_(ticket.shown),
                models.OfferView.delivered_at.is_(None),
            )
            .values(delivered_at=at)
        )
        if ticket.request_id is not None:
            await self._session.execute(
                update(_table(models.ClientRequest))
                .where(
                    models.ClientRequest.id == ticket.request_id,
                    models.ClientRequest.user_id == ticket.user_id,
                )
                .values(shown_count=models.ClientRequest.shown_count + len(ticket.shown))
            )

    async def release(self, ticket: Ticket) -> int:
        """Отправка не удалась: вернуть слоты. Трогает только свои неподтверждённые строки.

        «Виденные» (`repeated`) чужие: их записал более ранний показ, и откатывать
        его отправкой, которая не состоялась, нельзя.
        """
        if not ticket.granted:
            return 0
        removed = await self._session.execute(
            delete(_table(models.OfferView))
            .where(
                models.OfferView.period_id == ticket.period_id,
                models.OfferView.user_id == ticket.user_id,
                models.OfferView.listing_id.in_(ticket.granted),
                models.OfferView.delivered_at.is_(None),
            )
            .returning(models.OfferView.id)
        )
        return len(removed.scalars().all())

    async def sweep_stale(self, *, older_than: datetime, limit: int) -> int:
        """Снять зависшие резервы: процесс умер между записью и отправкой.

        Пачкой и `SKIP LOCKED`: резерв, который сейчас подтверждает живой процесс,
        не ждём и не трогаем, а возьмём на следующем проходе.
        """
        stale = (
            select(models.OfferView.id)
            .where(
                models.OfferView.delivered_at.is_(None),
                models.OfferView.shown_at < older_than,
                models.OfferView.channel.in_(SWEPT_CHANNELS),
            )
            .order_by(models.OfferView.shown_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        removed = await self._session.execute(
            delete(_table(models.OfferView))
            .where(models.OfferView.id.in_(stale.scalar_subquery()))
            .returning(models.OfferView.id)
        )
        return len(removed.scalars().all())

    async def usage(self, user_id: int, now: datetime) -> Usage:
        """Сколько занято в текущем периоде. Только чтение: ничего не создаёт и не блокирует."""
        anchor = await self._session.scalar(
            select(models.User.quota_anchor_at).where(models.User.id == user_id)
        )
        if anchor is None:
            return Usage(used=0, period_end=None)
        number, period = numbered_period(anchor, now)
        used = await self._session.scalar(
            select(func.count())
            .select_from(models.OfferView)
            .join(models.QuotaPeriod, models.QuotaPeriod.id == models.OfferView.period_id)
            .where(
                models.QuotaPeriod.user_id == user_id,
                models.QuotaPeriod.period_no == number,
                models.OfferView.channel != Channel.MONITOR.value,
            )
        )
        return Usage(used=int(used or 0), period_end=period.end)

    async def identify(self, refs: Sequence[tuple[str, str]]) -> dict[tuple[str, str], int]:
        """Карточки по паре «источник, внешний id» — так лот называет сам источник.

        Нужен тем, кто получил находки без `listing_id` (живой поиск, отложенный ответ из
        каталога наблюдений): журнал считает по карточке, и сначала её надо найти.
        """
        if not refs:
            return {}
        rows = await self._session.execute(
            select(models.Listing.id, models.Listing.source, models.Listing.external_id).where(
                tuple_(models.Listing.source, models.Listing.external_id).in_(list(refs))
            )
        )
        return {
            (source, external_id): int(listing_id)
            for listing_id, source, external_id in rows.all()
            if external_id is not None
        }
