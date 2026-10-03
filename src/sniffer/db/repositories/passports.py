"""Паспорт запроса: версии, а не перезапись.

Клиент говорит «дорого» — появляется новая версия с изменённым бюджетом,
старая остаётся. Без этого нельзя ни отладить агента, ни объяснить клиенту,
почему выдача изменилась (architecture.md, раздел 5).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, exists, func, or_, select, update

from sniffer.db import models
from sniffer.db.mappers import passport_values, to_passport_event, to_stored_passport
from sniffer.db.repositories.base import Repository
from sniffer.domain.passport import Passport
from sniffer.domain.records import PassportEvent, QueryOverview, StoredPassport
from sniffer.domain.threads import MAX_LIVE_THREADS


def not_archived() -> ColumnElement[bool]:
    """Поиск не убран человеком («Удалить поиск» = пауза и архив, версии остаются).

    Один предикат на список, выбор текущего и поиск по корню: убранный поиск не должен
    вернуться ни как «текущий», ни как строка меню, ни как цель кнопки со старого сообщения.
    """
    chain = func.coalesce(models.Passport.root_id, models.Passport.id)
    return ~exists().where(
        models.SearchTab.user_id == models.Passport.user_id,
        models.SearchTab.passport_root == chain,
        models.SearchTab.state == models.tabs.ARCHIVED,
    )


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
        conditions = [
            models.Passport.user_id == user_id,
            models.Passport.is_current.is_(True),
            not_archived(),
        ]
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
        """Выбрать свою цепочку; чужой root не меняет состояние.

        Выбор ветки снимает и взведённое `/new`: человек сказал, с какой веткой
        работает, и ждать от него новую просьбу больше незачем. Снимается здесь,
        а не у вызывающих, потому что через это место проходят ВСЕ действия над
        веткой — выбор, создание (`save_new`) и правка (`save_revision`). Снимай
        флаг у вызывающих — один из них забудут, и `/new` остался бы взведённым
        навсегда. Здесь же ветка поднимается в списке (`last_used_at`).
        """
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
                awaiting_new_request=False,
            )
        )
        # Использование поднимает поиск в списке — по этому и работает обещание
        # «выбор возвращает поиск в список». `clock_timestamp`, а не `now`: `now`
        # стоит на начале транзакции, и два выбора в одной транзакции были бы
        # неразличимы по порядку.
        await self._session.execute(
            update(models.Passport)
            .where(
                models.Passport.user_id == user_id,
                chain == root,
                models.Passport.is_current.is_(True),
            )
            .values(last_used_at=func.clock_timestamp())
        )
        return True

    async def await_new_request(self, user_id: int) -> None:
        """`/new` без текста: следующее сообщение открывает ветку, а не уточняет.

        Флаг, а не «ничего не делаем и надеемся»: между командой и сообщением бот
        перезапускается, а у голосового запроса между ними ещё и расшифровка.
        """
        await self._session.execute(
            update(models.User).where(models.User.id == user_id).values(awaiting_new_request=True)
        )

    async def consume_new_request(self, user_id: int) -> bool:
        """Потратить взведённое `/new`: `True` — флаг был взведён и теперь снят.

        Один `UPDATE … WHERE awaiting_new_request RETURNING`, а не «прочитал —
        снял»: два сообщения подряд после `/new` читают флаг оба, пока разбор
        первого ещё идёт, и без сравнения-и-замены оба открыли бы по поиску.
        Второй `UPDATE` ждёт блокировку строки первого, а после коммита видит
        снятый флаг и ничего не возвращает — проигравший знает, что поиск уже
        открыт.
        """
        spent = await self._session.execute(
            update(models.User)
            .where(models.User.id == user_id, models.User.awaiting_new_request.is_(True))
            .values(awaiting_new_request=False)
            .returning(models.User.id)
        )
        return spent.scalar_one_or_none() is not None

    async def clear_editing(self, user_id: int) -> None:
        await self._session.execute(
            update(models.User).where(models.User.id == user_id).values(editing_passport_root=None)
        )

    async def list_queries(
        self, user_id: int, *, limit: int = MAX_LIVE_THREADS
    ) -> list[QueryOverview]:
        """Поиски в работе и их мониторинги: недавно использованные сверху, не больше предела.

        Предел — в SQL, а не в отрисовке меню: обрезать список после выборки
        значило бы тянуть из базы все цепочки человека ради пяти строк, а главное
        — «сколько поисков в работе» перестало бы быть одним ответом. Порядок — по
        времени последнего использования (`last_used_at`, пока его нет — по
        `created_at`): выбор вытесненного поиска возвращает его в список и
        вытесняет самый давно не использованный. Вытесненный поиск не удаляется:
        его мониторинг читает `subscriptions` по корню и о списке не знает, а
        управлять им можно по корню (`get_query`), а не по членству в списке.
        """
        recency = func.coalesce(models.Passport.last_used_at, models.Passport.created_at)
        rows = await self._session.execute(
            _overviews(user_id).order_by(recency.desc(), models.Passport.id.desc()).limit(limit)
        )
        return [_overview(*row) for row in rows]

    async def get_query(self, user_id: int, root: int) -> QueryOverview | None:
        """Один поиск клиента по корню — независимо от того, помещается ли он в список.

        Принадлежность проверяется здесь, а не членством в обрезанном списке:
        вытесненный поиск остаётся поиском клиента, и пауза, «Искать снова» и
        «Изменить» обязаны работать на нём так же, как на видимом. Чужой или
        несуществующий корень — `None`.
        """
        chain = func.coalesce(models.Passport.root_id, models.Passport.id)
        found = await self._session.execute(_overviews(user_id).where(chain == root).limit(1))
        row = found.first()
        return None if row is None else _overview(*row)

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


def _overviews(user_id: int) -> Select[tuple[models.Passport, models.Subscription, int | None]]:
    """Текущие версии цепочек клиента с их подпиской и указателем активной.

    Один запрос на `list_queries` и `get_query`: два места, которые отвечают на
    вопрос «что за поиск и что с его мониторингом», обязаны отвечать одинаково.
    """
    chain = func.coalesce(models.Passport.root_id, models.Passport.id)
    return (
        select(models.Passport, models.Subscription, models.User.active_passport_root)
        .join(models.User, models.User.id == models.Passport.user_id)
        .outerjoin(
            models.Subscription,
            and_(
                models.Subscription.user_id == user_id, models.Subscription.passport_root == chain
            ),
        )
        .where(
            models.Passport.user_id == user_id,
            models.Passport.is_current.is_(True),
            not_archived(),
        )
    )


def _overview(
    passport: models.Passport, subscription: models.Subscription | None, active_root: int | None
) -> QueryOverview:
    root = passport.root_id or passport.id
    monitoring = "off"
    expires_at = None
    if subscription is not None:
        expires_at = subscription.expires_at
        if expires_at is not None and expires_at <= datetime.now(UTC):
            monitoring = "expired"
        else:
            monitoring = "active" if subscription.is_active else "paused"
    return QueryOverview(
        root=root,
        passport=to_stored_passport(passport).passport,
        is_active=root == active_root,
        monitoring=monitoring,
        expires_at=expires_at,
    )
