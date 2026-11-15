"""SQL уборки каталога: первоисточники и слияние дублей (`worker/primary_cleanup.py`).

Читает только существующие с 001/003 колонки — поэтому `--dry-run` работает и на
базе, куда миграция 026 ещё не доехала. Пишет — только при `--apply`, короткими
транзакциями, которые открывает и коммитит вызывающий.

Каждая запись охраняется условием «строка всё ещё такая, какой её видел план»
(`is_active`, прежняя ссылка). Поэтому повторный прогон ничего не делает дважды,
а гонка с воркером проигрывается тихо: строка, которую успели поменять, просто
пропускается и попадёт в следующий прогон.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text

from sniffer.db.repositories.base import Repository
from sniffer.domain.listing_dedup import PRICE_ATTRIBUTES
from sniffer.domain.listing_state import REASON_DUPLICATE_MERGED, REASON_NOT_PRIMARY
from sniffer.domain.origin import PostRef

_SNAPSHOT = (
    "jsonb_build_object('tg_link',tg_link,'price_amount',price_amount,"
    "'price_currency',price_currency,'price_period',price_period,"
    "'price_usd_month',price_usd_month,'posted_at',posted_at,'is_active',is_active)"
)


class PrimaryCleanupRepository(Repository):
    async def cards(self, *, after_id: int, limit: int) -> list[dict[str, Any]]:
        """Активные карточки архива Telegram с текстом своего сырья, по возрастанию id."""
        rows = await self._session.execute(
            text(
                """
                SELECT l.id, r.posted_at, l.city, l.category, l.deal_type, l.tg_link,
                       l.price_amount, l.price_currency, l.price_period, l.price_usd_month,
                       r.text
                FROM listings l JOIN raw_messages r ON r.id=l.raw_message_id
                WHERE l.is_active AND l.id>:after_id
                ORDER BY l.id LIMIT :limit
                """
            ),
            {"after_id": after_id, "limit": limit},
        )
        return [dict(row) for row in rows.mappings()]

    async def earlier_copies(self) -> list[dict[str, Any]]:
        """Карточки, привязанные к поздней копии, когда в сырье есть более ранняя.

        Прежний вход отдавал карточку свежему перепосту целиком, со ссылкой. Ранняя
        копия — тот же текст (отпечаток совпадает), опубликованный раньше в чате с
        публичным именем. Если у неё уже есть своя (погашенная) карточка — перенос
        невозможен (`UNIQUE raw_message_id`), такие строки отдаются с `blocked`.
        """
        rows = await self._session.execute(
            text(
                """
                SELECT DISTINCT ON (l.id)
                       l.id AS listing_id, l.tg_link AS before, l.raw_message_id AS old_raw_id,
                       l.external_id AS old_external_id,
                       e.id AS raw_id, e.chat_tg_id, e.msg_id, c.username,
                       e.chat_tg_id=r.chat_tg_id AS same_chat,
                       EXISTS (SELECT 1 FROM listings x
                               WHERE x.raw_message_id=e.id
                                  OR (x.source=l.source
                                      AND x.external_id=e.chat_tg_id || ':' || e.msg_id))
                           AS blocked
                FROM listings l
                JOIN raw_messages r ON r.id=l.raw_message_id
                JOIN raw_messages e ON e.text_hash=r.text_hash AND e.id<>r.id
                     AND (e.posted_at, e.id) < (r.posted_at, r.id)
                JOIN chats c ON c.tg_id=e.chat_tg_id AND c.username IS NOT NULL
                WHERE l.is_active
                ORDER BY l.id, e.posted_at, e.id
                """
            )
        )
        return [dict(row) for row in rows.mappings()]

    async def rebind(self, row: dict[str, Any], after: str) -> bool:
        """Перенести карточку на раннюю копию: ссылка, сырьё и внешний id — вместе."""
        result = await self._session.execute(
            text(
                """
                UPDATE listings SET raw_message_id=:raw_id, tg_link=:after,
                       external_id=:external_id
                WHERE id=:id AND is_active AND raw_message_id=:old_raw_id AND tg_link=:before
                  AND NOT EXISTS (
                      SELECT 1 FROM listings x
                      WHERE x.raw_message_id=:raw_id
                         OR (x.source=listings.source AND x.external_id=:external_id))
                RETURNING id
                """
            ),
            {
                "id": row["listing_id"],
                "raw_id": row["raw_id"],
                "old_raw_id": row["old_raw_id"],
                "external_id": f"{row['chat_tg_id']}:{row['msg_id']}",
                "before": row["before"],
                "after": after,
            },
        )
        if result.scalar_one_or_none() is None:
            return False
        # Снимок «как было» известен из плана: строка сменилась ровно на эти три поля.
        await self._session.execute(
            text(
                """
                INSERT INTO listing_cleanup_events
                    (listing_id, canonical_listing_id, action, before)
                VALUES (:id, NULL, 'earlier_copy', jsonb_build_object(
                    'tg_link', CAST(:before AS text),
                    'raw_message_id', CAST(:old_raw_id AS bigint),
                    'external_id', CAST(:old_external_id AS text)))
                """
            ),
            {
                "id": row["listing_id"],
                "before": row["before"],
                "old_raw_id": row["old_raw_id"],
                "old_external_id": row["old_external_id"],
            },
        )
        return True

    async def resolve_post(self, post: PostRef) -> tuple[str | None, int | None]:
        """Ссылка на оригинал и его активная карточка, если оригинал у нас есть."""
        row = (
            (
                await self._session.execute(
                    text(
                        """
                        SELECT c.username, l.id AS listing_id
                        FROM chats c
                        LEFT JOIN raw_messages r ON r.chat_tg_id=c.tg_id AND r.msg_id=:msg_id
                        LEFT JOIN listings l ON l.raw_message_id=r.id AND l.is_active
                        WHERE c.tg_id=:chat_tg_id OR lower(c.username)=:username
                        ORDER BY l.id NULLS LAST LIMIT 1
                        """
                    ),
                    {
                        "msg_id": post.msg_id,
                        "chat_tg_id": post.chat_tg_id,
                        "username": (post.username or "").lower() or None,
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return post.url, None
        known = PostRef(post.msg_id, post.chat_tg_id, row["username"] or post.username)
        return known.url, row["listing_id"]

    async def merge(
        self,
        canonical_id: int,
        duplicate_ids: tuple[int, ...],
        price_from_id: int | None,
        posted_at: datetime,
    ) -> bool:
        """Слить группу. `False` — первоисточник уже не активен, план устарел."""
        alive = await self._session.scalar(
            text("SELECT id FROM listings WHERE id=:id AND is_active FOR UPDATE"),
            {"id": canonical_id},
        )
        if alive is None:
            return False
        pending = await self._session.scalar(
            text("SELECT count(*) FROM listings WHERE id = ANY(:ids) AND is_active"),
            {"ids": list(duplicate_ids)},
        )
        if not pending:
            return False  # группа уже слита: повтор не пишет ни цену, ни событие
        await self._event(canonical_id, canonical_id, "merge_canonical")
        await self._session.execute(
            text(
                """
                UPDATE listings c SET
                    price_amount=COALESCE(d.price_amount, c.price_amount),
                    price_currency=CASE WHEN d.id IS NULL THEN c.price_currency
                                        ELSE d.price_currency END,
                    price_period=CASE WHEN d.id IS NULL THEN c.price_period
                                      ELSE d.price_period END,
                    price_usd_month=CASE WHEN d.id IS NULL THEN c.price_usd_month
                                         ELSE d.price_usd_month END,
                    -- Цена вне колонок (вилка, ставка) едет вместе с ценой.
                    attributes=CASE WHEN d.id IS NULL THEN c.attributes
                        ELSE (c.attributes - CAST(:price_keys AS text[])) || COALESCE(
                            (SELECT jsonb_object_agg(e.key, e.value)
                             FROM jsonb_each(d.attributes) AS e
                             WHERE e.key = ANY(CAST(:price_keys AS text[]))),
                            '{}'::jsonb) END,
                    posted_at=GREATEST(c.posted_at, :posted_at)
                FROM (SELECT 1) AS one
                LEFT JOIN listings d ON d.id=:donor AND d.price_amount IS NOT NULL
                WHERE c.id=:id
                """
            ),
            {
                "id": canonical_id,
                "donor": price_from_id,
                "posted_at": posted_at,
                "price_keys": sorted(PRICE_ATTRIBUTES),
            },
        )
        for duplicate in duplicate_ids:
            await self.deactivate(duplicate, canonical_id, REASON_DUPLICATE_MERGED)
        return True

    async def rewrite_link(self, listing_id: int, before: str, after: str, action: str) -> bool:
        await self._event(listing_id, None, action, guard_link=before)
        result = await self._session.execute(
            text(
                "UPDATE listings SET tg_link=:after "
                "WHERE id=:id AND is_active AND tg_link=:before RETURNING id"
            ),
            {"id": listing_id, "before": before, "after": after},
        )
        return result.scalar_one_or_none() is not None

    async def deactivate(
        self, listing_id: int, canonical_id: int | None, reason: str = REASON_NOT_PRIMARY
    ) -> bool:
        await self._event(listing_id, canonical_id, reason)
        result = await self._session.execute(
            text(
                "UPDATE listings SET is_active=false, deactivated_at=now(), "
                "deactivated_reason=:reason WHERE id=:id AND is_active RETURNING id"
            ),
            {"id": listing_id, "reason": reason},
        )
        return result.scalar_one_or_none() is not None

    async def _event(
        self,
        listing_id: int,
        canonical_id: int | None,
        action: str,
        *,
        guard_link: str | None = None,
    ) -> None:
        """Снимок «как было» ДО записи; пишется, только если строка ещё в ожидаемом виде."""
        await self._session.execute(
            text(
                f"""
                INSERT INTO listing_cleanup_events
                    (listing_id, canonical_listing_id, action, before)
                SELECT id, :canonical_id, :action, {_SNAPSHOT}
                FROM listings
                WHERE id=:id AND is_active AND (CAST(:guard AS text) IS NULL OR tg_link=:guard)
                """  # noqa: S608 -- снимок — литерал модуля, значения связаны
            ),
            {"id": listing_id, "canonical_id": canonical_id, "action": action, "guard": guard_link},
        )

    async def publications(self) -> list[dict[str, Any]]:
        """Проверенный каталог: ключи считаются по наблюдению, на которое смотрит публикация."""
        rows = await self._session.execute(
            text(
                """
                SELECT p.source, p.external_id, p.city, p.category, p.deal_type, p.fetched_at,
                       o.payload->>'url' AS url, o.payload->>'raw_text' AS raw_text,
                       o.payload->>'posted_at' AS posted_at
                FROM catalog_publications p
                JOIN catalog_observations o ON o.id=p.observation_id
                WHERE p.active
                ORDER BY p.source, p.external_id
                """
            )
        )
        return [dict(row) for row in rows.mappings()]

    async def set_publication_identity(
        self,
        source: str,
        external_id: str,
        keys: list[str],
        duplicate_of: tuple[str, str] | None,
    ) -> None:
        await self._session.execute(
            text(
                """
                UPDATE catalog_publications SET identity_keys=CAST(:keys AS text[]),
                    duplicate_of_source=:dup_source, duplicate_of_external_id=:dup_external
                WHERE source=:source AND external_id=:external_id
                """
            ),
            {
                "keys": keys,
                "source": source,
                "external_id": external_id,
                "dup_source": duplicate_of[0] if duplicate_of else None,
                "dup_external": duplicate_of[1] if duplicate_of else None,
            },
        )
