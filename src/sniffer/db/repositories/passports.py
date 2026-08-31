"""Паспорт запроса: версии, а не перезапись.

Клиент говорит «дорого» — появляется новая версия с изменённым бюджетом,
старая остаётся. Без этого нельзя ни отладить агента, ни объяснить клиенту,
почему выдача изменилась (architecture.md, раздел 5).
"""

from __future__ import annotations

from sqlalchemy import or_, select, update

from sniffer.db import models
from sniffer.db.mappers import passport_values, to_stored_passport
from sniffer.db.repositories.base import Repository
from sniffer.domain.passport import Passport
from sniffer.domain.records import StoredPassport


class PassportRepository(Repository):
    async def get(self, passport_id: int) -> StoredPassport | None:
        row = await self._session.get(models.Passport, passport_id)
        return to_stored_passport(row) if row is not None else None

    async def get_current(self, user_id: int) -> StoredPassport | None:
        """Паспорт, с которым клиент работает прямо сейчас.

        У пользователя может быть несколько цепочек версий — по одной на
        каждый свой запрос, — поэтому «текущий» это самый свежий из актуальных,
        а не единственный.
        """
        row = await self._session.scalar(
            select(models.Passport)
            .where(models.Passport.user_id == user_id, models.Passport.is_current.is_(True))
            .order_by(models.Passport.created_at.desc(), models.Passport.id.desc())
            .limit(1)
        )
        return to_stored_passport(row) if row is not None else None

    async def save_new(self, user_id: int, passport: Passport) -> StoredPassport:
        """Первая версия цепочки: `root_id` пустой, корнем служит свой же id."""
        row = models.Passport(user_id=user_id, version=1, root_id=None, **passport_values(passport))
        self._session.add(row)
        await self._session.flush()
        return to_stored_passport(row)

    async def save_revision(self, previous: StoredPassport, passport: Passport) -> StoredPassport:
        """Следующая версия того же запроса.

        Прежние версии цепочки снимаются с `is_current` до вставки новой:
        иначе подписка и выдача успели бы увидеть две актуальные версии одного
        паспорта и разойтись в том, какая из них правда.
        """
        root = previous.root
        await self._session.execute(
            update(models.Passport)
            .where(or_(models.Passport.id == root, models.Passport.root_id == root))
            .values(is_current=False)
        )
        row = models.Passport(
            user_id=previous.user_id,
            version=previous.version + 1,
            root_id=root,
            **passport_values(passport),
        )
        self._session.add(row)
        await self._session.flush()
        return to_stored_passport(row)
