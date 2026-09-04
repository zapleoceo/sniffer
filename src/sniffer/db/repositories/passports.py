"""Паспорт запроса: версии, а не перезапись.

Клиент говорит «дорого» — появляется новая версия с изменённым бюджетом,
старая остаётся. Без этого нельзя ни отладить агента, ни объяснить клиенту,
почему выдача изменилась (architecture.md, раздел 5).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select, update

from sniffer.db import models
from sniffer.db.mappers import passport_values, to_passport_event, to_stored_passport
from sniffer.db.repositories.base import Repository
from sniffer.domain.passport import Passport
from sniffer.domain.records import PassportEvent, QueryOverview, StoredPassport


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
        active = await self._session.scalar(
            select(models.User.active_passport_root).where(models.User.id == user_id)
        )
        chain = func.coalesce(models.Passport.root_id, models.Passport.id)
        conditions = [models.Passport.user_id == user_id, models.Passport.is_current.is_(True)]
        if active is not None:
            conditions.append(chain == active)
        row = await self._session.scalar(
            select(models.Passport)
            .where(*conditions)
            .order_by(models.Passport.created_at.desc(), models.Passport.id.desc())
            .limit(1)
        )
        if active is None and row is not None:
            await self._session.execute(
                update(models.User)
                .where(models.User.id == user_id)
                .values(active_passport_root=row.root_id or row.id)
            )
        return to_stored_passport(row) if row is not None else None

    async def select(self, user_id: int, root: int, *, editing: bool = False) -> bool:
        """Выбрать свою цепочку; чужой root не меняет состояние."""
        chain = func.coalesce(models.Passport.root_id, models.Passport.id)
        owned = await self._session.scalar(
            select(models.Passport.id)
            .where(models.Passport.user_id == user_id, chain == root)
            .limit(1)
        )
        if owned is None:
            return False
        await self._session.execute(
            update(models.User)
            .where(models.User.id == user_id)
            .values(
                active_passport_root=root,
                editing_passport_root=root if editing else None,
            )
        )
        return True

    async def clear_editing(self, user_id: int) -> None:
        await self._session.execute(
            update(models.User).where(models.User.id == user_id).values(editing_passport_root=None)
        )

    async def list_queries(self, user_id: int) -> list[QueryOverview]:
        """Все актуальные цепочки и их мониторинги, свежие сверху."""
        chain = func.coalesce(models.Passport.root_id, models.Passport.id)
        rows = await self._session.execute(
            select(models.Passport, models.Subscription, models.User.active_passport_root)
            .join(models.User, models.User.id == models.Passport.user_id)
            .outerjoin(
                models.Subscription,
                and_(
                    models.Subscription.user_id == user_id,
                    models.Subscription.passport_root == chain,
                ),
            )
            .where(models.Passport.user_id == user_id, models.Passport.is_current.is_(True))
            .order_by(models.Passport.created_at.desc(), models.Passport.id.desc())
        )
        result: list[QueryOverview] = []
        moment = datetime.now(UTC)
        for passport, subscription, active_root in rows:
            root = passport.root_id or passport.id
            monitoring = "off"
            expires_at = None
            if subscription is not None:
                expires_at = subscription.expires_at
                if expires_at is not None and expires_at <= moment:
                    monitoring = "expired"
                else:
                    monitoring = "active" if subscription.is_active else "paused"
            result.append(
                QueryOverview(
                    root=root,
                    passport=to_stored_passport(passport).passport,
                    is_active=root == active_root,
                    monitoring=monitoring,
                    expires_at=expires_at,
                )
            )
        return result

    async def save_new(self, user_id: int, passport: Passport) -> StoredPassport:
        """Первая версия цепочки: `root_id` пустой, корнем служит свой же id."""
        row = models.Passport(user_id=user_id, version=1, root_id=None, **passport_values(passport))
        self._session.add(row)
        await self._session.flush()
        await self.select(user_id, row.id)
        return to_stored_passport(row)

    async def save_revision(self, previous: StoredPassport, passport: Passport) -> StoredPassport:
        """Следующая версия того же запроса.

        Прежние версии цепочки снимаются с `is_current` до вставки новой:
        иначе подписка и выдача успели бы увидеть две актуальные версии одного
        паспорта и разойтись в том, какая из них правда.

        Цепочка блокируется по корню, и номер версии считается уже под
        блокировкой, а не берётся из `previous`. `previous` прочитан другой
        сессией и к этому моменту может быть устаревшим: два одновременных
        уточнения (двойной тап по кнопке, ретрай апдейта Telegram, два воркера)
        иначе оба посчитали бы `previous.version + 1` от одной и той же версии
        и вставили два одинаковых номера, а `is_current` при неудачном
        переплетении остался бы у обоих. Postgres по умолчанию READ COMMITTED —
        сам он такую пару не разведёт.
        """
        root = previous.root
        await self._session.execute(
            select(models.Passport.id).where(models.Passport.id == root).with_for_update()
        )
        latest = await self._session.scalar(
            select(func.max(models.Passport.version)).where(
                or_(models.Passport.id == root, models.Passport.root_id == root)
            )
        )
        await self._session.execute(
            update(models.Passport)
            .where(or_(models.Passport.id == root, models.Passport.root_id == root))
            .values(is_current=False)
        )
        row = models.Passport(
            user_id=previous.user_id,
            version=(latest or previous.version) + 1,
            root_id=root,
            **passport_values(passport),
        )
        self._session.add(row)
        await self._session.flush()
        await self.select(previous.user_id, root)
        return to_stored_passport(row)

    async def list_versions(self, root: int) -> list[StoredPassport]:
        """Вся цепочка версий по возрастанию номера.

        Нужна там, где важна цепочка целиком, а не её последняя версия: объяснить
        клиенту, почему выдача изменилась, и проверить инвариант «одна актуальная
        версия на цепочку», который в одиночной выборке не виден.
        """
        rows = await self._session.scalars(
            select(models.Passport)
            .where(or_(models.Passport.id == root, models.Passport.root_id == root))
            .order_by(models.Passport.version, models.Passport.id)
        )
        return [to_stored_passport(row) for row in rows]

    async def add_event(
        self, passport_id: int, kind: str, payload: dict[str, Any] | None = None
    ) -> PassportEvent:
        """След диалога: заданный вопрос, ответ клиента, нажатая кнопка."""
        row = models.PassportEvent(passport_id=passport_id, kind=kind, payload=payload or {})
        self._session.add(row)
        await self._session.flush()
        return to_passport_event(row)

    async def list_events(self, root: int) -> list[PassportEvent]:
        """События всей цепочки версий, в порядке появления.

        Именно цепочки, а не одной версии: клиент отвечает на вопрос — версия
        меняется, а счётчик заданных вопросов обязан продолжиться, а не
        начаться заново.
        """
        rows = await self._session.scalars(
            select(models.PassportEvent)
            .join(models.Passport, models.Passport.id == models.PassportEvent.passport_id)
            .where(or_(models.Passport.id == root, models.Passport.root_id == root))
            .order_by(models.PassportEvent.id)
        )
        return [to_passport_event(row) for row in rows]
