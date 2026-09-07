"""Atomic projection of verified catalog facts into the delivery catalog."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import cast

from sqlalchemy import Table, literal, select, update
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import JSONB, insert

from sniffer.db import models
from sniffer.db.repositories.base import Repository
from sniffer.domain.catalog import CatalogObservation


class CatalogListingProjectionRepository(Repository):
    async def project(self, observation_id: int, observed: CatalogObservation) -> int:
        facts = observed.facts
        if not observed.publishable:
            raise ValueError("unpublishable_projection")
        legacy_listing_id = await self._legacy_listing_id(observed)
        attributes = {
            key: value
            for key in ("brand", "model", "transmission", "engine_cc", "rooms", "furnished")
            if (value := getattr(facts, key)) is not None
        }
        values = {
            # Archive keeps the legacy raw-bound card intact. The verified
            # projection needs a new monotonic id so an already advanced matcher
            # can reconsider it; source/external_id remains its idempotency key.
            "raw_message_id": None,
            "catalog_observation_id": observation_id,
            "source": observed.source,
            "external_id": observed.external_id,
            "deal_type": facts.deal_type,
            "category": facts.category.value if facts.category is not None else None,
            "city": facts.city,
            "title": observed.title,
            "summary": observed.raw_text,
            "price_amount": Decimal(facts.price_vnd) if facts.price_vnd is not None else None,
            "price_currency": "VND" if facts.price_vnd is not None else None,
            "price_period": facts.price_period.value if facts.price_period is not None else None,
            "attributes": attributes,
            "tg_link": observed.url,
            "confidence": 1.0,
            "posted_at": observed.posted_at or observed.fetched_at,
            "is_active": facts.active,
        }
        table = cast(Table, models.Listing.__table__)
        statement = insert(table).values(**values)
        projected = await self._session.scalar(
            statement.on_conflict_do_update(
                index_elements=["source", "external_id"],
                set_={key: statement.excluded[key] for key in values if key != "raw_message_id"},
            ).returning(table.c.id)
        )
        if projected is None:
            raise RuntimeError("catalog_projection_failed")
        projected_id = int(projected)
        if legacy_listing_id is not None:
            await self._carry_delivery_state(
                legacy_listing_id,
                projected_id,
                {
                    "listing_id": projected_id,
                    "title": observed.title,
                    "summary": observed.raw_text,
                    "url": observed.url,
                    "price_amount": str(facts.price_vnd) if facts.price_vnd is not None else "",
                    "price_currency": "VND" if facts.price_vnd is not None else "",
                    "posted_at": (observed.posted_at or observed.fetched_at).isoformat(),
                },
            )
        return projected_id

    async def _legacy_listing_id(self, observed: CatalogObservation) -> int | None:
        if observed.source != "archive":
            return None
        parts = observed.external_id.split(":", 1)
        if len(parts) != 2 or not all(part.lstrip("-").isdigit() for part in parts):
            raise ValueError("invalid_archive_identity")
        chat_tg_id, msg_id = map(int, parts)
        identifier = await self._session.scalar(
            select(models.Listing.id)
            .join(models.RawMessage, models.Listing.raw_message_id == models.RawMessage.id)
            .where(
                models.RawMessage.chat_tg_id == chat_tg_id,
                models.RawMessage.msg_id == msg_id,
            )
        )
        if identifier is None:
            raise ValueError("archive_original_missing")
        return int(identifier)

    async def _carry_delivery_state(
        self, legacy_id: int, projected_id: int, payload_patch: dict[str, object]
    ) -> None:
        """Move pending delivery; copy only markers which were actually sent."""
        table = cast(Table, models.Notification.__table__)
        # The outbox references the notification, not the listing. Rebinding an
        # unsent notification therefore preserves its one existing delivery job.
        moved = await self._session.scalars(
            update(table)
            .where(table.c.listing_id == legacy_id, table.c.sent_at.is_(None))
            .values(listing_id=projected_id)
            .returning(table.c.id)
        )
        moved_ids = list(moved)
        if moved_ids:
            patch = sql_cast(literal(json.dumps(payload_patch, ensure_ascii=False)), JSONB)
            await self._session.execute(
                update(models.Outbox)
                .where(models.Outbox.notification_id.in_(moved_ids))
                .values(payload=models.Outbox.payload.op("||")(patch))
            )
        existing = select(
            table.c.subscription_id,
            literal(projected_id),
            table.c.score,
            table.c.created_at,
            table.c.sent_at,
        ).where(table.c.listing_id == legacy_id, table.c.sent_at.is_not(None))
        await self._session.execute(
            insert(table)
            .from_select(
                ["subscription_id", "listing_id", "score", "created_at", "sent_at"], existing
            )
            .on_conflict_do_nothing(index_elements=["subscription_id", "listing_id"])
        )
