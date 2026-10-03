"""Запись правки фильтра: одна новая версия с проверкой `base_version`.

Человек правит карточку фильтра, которую видел несколько минут назад; за это время версия
могла смениться (диалог, вторая правка с другого устройства). Применить его набор к чужой
версии значило бы молча затереть чужое условие, поэтому правка несёт номер версии, на
которую смотрел человек, и при расхождении отклоняется — карточка перерисовывается свежей.
Проверка идёт под той же блокировкой корня, что и `save_revision`, иначе между «сверил» и
«записал» вклинивалась бы другая правка.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select

from sniffer.db import models
from sniffer.db.repositories.base import Repository
from sniffer.db.repositories.passports import PassportRepository
from sniffer.domain.passport_edit import EVENT_KIND, Change, apply_changes
from sniffer.domain.records import StoredPassport


class StaleVersion(Exception):
    """Поиск изменился после того, как человек открыл карточку."""

    def __init__(self, current: StoredPassport) -> None:
        super().__init__(f"текущая версия {current.version}")
        self.current = current


class PassportEditor(Repository):
    async def edit(
        self, *, user_id: int, root: int, base_version: int, changes: Sequence[Change]
    ) -> StoredPassport:
        """`LookupError` — чужой или несуществующий поиск; `StaleVersion`; `EditError`."""
        passports = PassportRepository(self._session)
        await self._session.execute(
            select(models.Passport.id).where(models.Passport.id == root).with_for_update()
        )
        current = await self._current(passports, user_id, root)
        if current.version != base_version:
            raise StaleVersion(current)
        revised, payload = apply_changes(current.passport, changes)
        stored = await passports.save_revision(current, revised)
        await passports.add_event(stored.id, EVENT_KIND, payload)
        return stored

    async def current(self, *, user_id: int, root: int) -> StoredPassport:
        return await self._current(PassportRepository(self._session), user_id, root)

    async def _current(
        self, passports: PassportRepository, user_id: int, root: int
    ) -> StoredPassport:
        found = await passports.get_query(user_id, root)
        if found is None:
            raise LookupError(f"поиск {root} не найден у клиента {user_id}")
        row = await self._session.scalar(
            select(models.Passport).where(
                models.Passport.is_current.is_(True),
                (models.Passport.id == root) | (models.Passport.root_id == root),
            )
        )
        if row is None:  # pragma: no cover — get_query уже нашёл текущую версию
            raise LookupError(f"у поиска {root} нет текущей версии")
        stored = await passports.get(row.id)
        assert stored is not None
        return stored
