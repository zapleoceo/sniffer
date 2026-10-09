"""Очередь доставки кандидатов в комнату агентов: что ещё не принято и куда сдвинут курсор.

Курсор свой (`room_relay_cursor`), а не `notifications.sent_at`: `sent_at` отвечает на вопрос
«дошло ли до клиента в Telegram», а здесь вопрос другой — «принято ли комнатой».
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from sniffer.db import models
from sniffer.db.repositories.base import Repository
from sniffer.domain.room_relay import RelayCandidate


class RoomRelayRepository(Repository):
    async def pending(self, subscription_ids: Sequence[int], limit: int) -> list[RelayCandidate]:
        """Уведомления подписок правее курсора, по возрастанию id (старые первыми)."""
        if not subscription_ids:
            return []
        n, c = models.Notification, models.RoomRelayCursor
        listing, raw = models.Listing, models.RawMessage
        media = (
            select(func.count())
            .where(models.ListingMedia.listing_id == listing.id)
            .scalar_subquery()
        )
        stmt = (
            select(n, listing, raw.text, raw.has_media, media)
            .join(listing, listing.id == n.listing_id)
            .outerjoin(raw, raw.id == listing.raw_message_id)
            .outerjoin(c, c.subscription_id == n.subscription_id)
            .where(
                n.subscription_id.in_(list(subscription_ids)),
                n.id > func.coalesce(c.last_notification_id, 0),
            )
            .order_by(n.id)
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            RelayCandidate(
                notification_id=note.id,
                subscription_id=note.subscription_id,
                score=float(note.score),
                tg_link=item.tg_link,
                posted_at=item.posted_at,
                price_amount=item.price_amount,
                price_currency=item.price_currency,
                price_period=item.price_period,
                district=item.district,
                city=item.city,
                title=item.title,
                summary=item.summary,
                attributes=dict(item.attributes or {}),
                text=text,
                has_media=bool(has_media) or count > 0,
                media_count=count,
            )
            for note, item, text, has_media, count in rows
        ]

    async def advance(self, subscription_id: int, notification_id: int) -> None:
        """Сдвинуть курсор вперёд; назад он не ходит (`GREATEST`). Коммит — за вызывающим."""
        c = models.RoomRelayCursor
        stmt = insert(c).values(
            subscription_id=subscription_id, last_notification_id=notification_id
        )
        await self._session.execute(
            stmt.on_conflict_do_update(
                index_elements=[c.subscription_id],
                set_={
                    "last_notification_id": func.greatest(
                        c.last_notification_id, stmt.excluded.last_notification_id
                    ),
                    "updated_at": func.now(),
                },
            )
        )
