"""Original public archive records; never recycled extracted listings."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from sniffer.db.repositories.base import Repository


class CollectionSourceRepository(Repository):
    async def archive(self, city: str, *, limit: int = 6) -> list[dict[str, Any]]:
        if city not in {"nha_trang", "da_nang"} or type(limit) is not int or not 1 <= limit <= 12:
            raise ValueError("invalid_archive_scope")
        result = await self._session.execute(
            text("""
            SELECT r.chat_tg_id,r.msg_id,r.text,r.posted_at,r.ingested_at,c.username
            FROM raw_messages r JOIN chats c ON c.tg_id=r.chat_tg_id
            WHERE c.is_active AND c.city=:city AND c.username ~ '^[A-Za-z0-9_]{5,32}$'
              AND r.text<>'' AND r.posted_at>clock_timestamp()-interval '30 days'
            ORDER BY r.posted_at DESC,r.id DESC LIMIT :limit
        """),
            {"city": city, "limit": limit},
        )
        return [dict(row) for row in result.mappings()]
