"""Request ownership and immutable-version checks for the MCP boundary."""

from __future__ import annotations

from sqlalchemy import func, select

from sniffer.db import models
from sniffer.db.mappers import to_stored_passport
from sniffer.db.repositories.base import Repository
from sniffer.domain.records import StoredPassport


class AgentRequestRepository(Repository):
    async def owned(self, user_id: int, root: int, version: int) -> StoredPassport:
        row = await self._session.scalar(
            select(models.Passport).where(
                models.Passport.user_id == user_id,
                func.coalesce(models.Passport.root_id, models.Passport.id) == root,
                models.Passport.version == version,
                models.Passport.is_current.is_(True),
            )
        )
        if row is None:
            raise PermissionError("request_not_current_or_owned")
        return to_stored_passport(row)
