"""Монитор подписок в базе: кого взять в обход, как отметить обход, кого изолировать.

Отдельно от `delivery.py` по причине, а не ради счётчика строк: там живут очередь доставки и
платежи, а здесь — состояние самого обхода (ротация и карантин). Второй ответственностью
в одном файле было бы то, что менять их приходится по разным поводам.

Четыре свойства, ради которых модуль есть:

* **Обход по кругу.** Порядок — «кого не смотрели дольше всех, первым», а не по `id`:
  прежний `ORDER BY id LIMIT 50` навсегда прятал 51-ю подписку (D4).
* **Больная строка не уносит остальных.** Паспорт с незнакомым значением перечисления
  падает при разборе; пачка, разбираемая списком, падала целиком, и воркер вместе с ней
  (D5). Здесь каждая строка разбирается отдельно, а больная возвращается с причиной.
* **Карантин по времени.** Сбойная подписка не выбирается до `quarantined_until`, потом
  выбирается снова сама: чинить её руками ради возвращения в обход не нужно.
* **Отмена просроченной очереди.** Найденное в оплаченный период доходит ещё льготный срок
  после окончания подписки, дальше строка `outbox` отменяется (`cancel_lapsed`, D7).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, Select, Update, func, or_, select, update

from sniffer.db import models
from sniffer.db.mappers import to_subscription_state
from sniffer.db.repositories.base import Repository
from sniffer.db.repositories.delivery import OUTBOX_CANCELLED, OUTBOX_PENDING, entitled
from sniffer.domain.monitoring import Overflow
from sniffer.domain.records import SubscriptionState

# Причина сбоя в базе — строка для человека, а не журнал: длинный разбор остаётся в логе
# процесса, а колонка не должна раздуваться текстом чужого исключения.
ERROR_LIMIT = 500


def describe_error(exc: BaseException) -> str:
    """Причина сбоя одной строкой — так её пишут и в колонку, и в журнал."""
    return f"{type(exc).__name__}: {exc}"[:ERROR_LIMIT]


@dataclass(frozen=True, slots=True)
class BrokenSubscription:
    """Подписка, которую не удалось прочитать: чем именно, и сколько раз подряд."""

    id: int
    user_id: int
    failed_streak: int
    error: str


@dataclass(frozen=True, slots=True)
class DueSubscriptions:
    """Порция обхода: что можно обслуживать и что прочитать не вышло."""

    ready: list[SubscriptionState]
    broken: list[BrokenSubscription]


def _servable(now: datetime) -> list[ColumnElement[bool]]:
    """Подписки, которые монитор вправе обслуживать, — ОДНО условие на выбор и на ранг.

    Ранг слота считается среди слотов, которые монитор действительно возьмёт: карантинный
    слот, заблокировавший бота клиент или цепочка без текущей версии паспорта не должны
    занимать место и отнимать суточный потолок у живого слота (ревью Opus, P5).
    """
    return [
        models.Passport.is_current.is_(True),
        entitled(now),
        # Клиент заблокировал бота: слать нечем, ставить в очередь незачем. Пауза выведена
        # запросом, а не записана в подписку: разблокировал — слежение возобновилось само,
        # ручная пауза (`is_active`), срок и курсор не тронуты.
        models.User.bot_blocked_at.is_(None),
        # Карантин кончается сам: строка с истёкшим сроком снова в обходе.
        or_(
            models.Subscription.quarantined_until.is_(None),
            models.Subscription.quarantined_until <= now,
        ),
    ]


def _chain() -> ColumnElement[int]:
    # Подписка хранит корень цепочки, а не версию: клиент правит запрос, и подписка обязана
    # следовать за правкой, а не застывать на версии, при которой её создали. Отсюда join
    # по `COALESCE(root_id, id)` и условие `is_current`.
    return func.coalesce(models.Passport.root_id, models.Passport.id)


def _ranked_statement(user_ids: Sequence[int], *, now: datetime) -> Select[tuple[int, int]]:
    return (
        select(models.Subscription.user_id, models.Subscription.id)
        .join(models.Passport, _chain() == models.Subscription.passport_root)
        .join(models.User, models.User.id == models.Subscription.user_id)
        .where(models.Subscription.user_id.in_(list(user_ids)), *_servable(now))
        .order_by(models.Subscription.user_id, models.Subscription.id)
    )


def _due_statement(
    *, limit: int, now: datetime
) -> Select[tuple[models.Subscription, models.Passport]]:
    return (
        select(models.Subscription, models.Passport)
        .join(models.Passport, _chain() == models.Subscription.passport_root)
        .join(models.User, models.User.id == models.Subscription.user_id)
        .where(*_servable(now))
        # NULLS FIRST: новая подписка (ни разу не смотрели) идёт раньше всех, а дальше —
        # по давности обхода. `id` разводит равные: порядок должен быть полным, иначе
        # две подписки с одним временем могли бы меняться местами от прохода к проходу.
        .order_by(models.Subscription.last_scanned_at.asc().nulls_first(), models.Subscription.id)
        .with_for_update(of=models.Subscription, skip_locked=True)
        .limit(limit)
        # Порция — снимок базы на момент блокировки, а не то, что сессия когда-то прочитала:
        # строка, уже лежащая в карте идентичности, иначе вернулась бы со старым
        # `failed_streak`, и пауза карантина считалась бы от устаревшего числа. В бою сессия
        # на проход новая, и разницы нет; но порция не должна зависеть от того, кто и когда
        # успел прочитать те же строки в этой же сессии.
        .execution_options(populate_existing=True)
    )


def _cancel_statement(*, cutoff: datetime) -> Update:
    # Просроченной считается подписка с ограниченным сроком, вышедшим РАНЬШЕ границы:
    # `NULL < …` — это NULL, то есть «бессрочная» сюда не попадает. Строки без подписки
    # (ответы сбора каталога) не попадают тоже: у них нет соединения с подписками.
    lapsed = select(models.Subscription.id).where(models.Subscription.expires_at < cutoff)
    # `SKIP LOCKED`: нотифаер держит взятые в отправку строки до конца пачки, и обычный
    # UPDATE по ним встал бы в очередь за его коммитом — на секунды, а проход матчера стоит
    # на пути всего воркера. Результат от этого не меняется: строка, которую нотифаер уже
    # отправляет, всё равно уйдёт (отменять отправляемое поздно), а остальное отменится
    # здесь же. Блокировать воркер ради строки, чья судьба решена, незачем.
    pending = (
        select(models.Outbox.id)
        .where(models.Outbox.status == OUTBOX_PENDING, models.Outbox.subscription_id.in_(lapsed))
        .with_for_update(skip_locked=True)
    )
    return (
        update(models.Outbox)
        .where(models.Outbox.id.in_(pending))
        .values(status=OUTBOX_CANCELLED)
        .returning(models.Outbox.id)
        .execution_options(synchronize_session=False)
    )


class MonitorRepository(Repository):
    async def claim_due(self, *, limit: int, now: datetime) -> DueSubscriptions:
        """Порция подписок на этот проход, заблокированная до конца транзакции.

        `FOR UPDATE SKIP LOCKED`: две копии воркера не берут одну подписку и не делят
        дважды её суточный лимит; занятую соседом строку пропускаем, а не ждём. Блокировка
        держится до коммита прохода, поэтому платёж, продляющий подписку, ждёт, а не
        меняет срок посреди обслуживания.

        `now` обязателен и без значения по умолчанию: молчаливый откат на часы процесса
        вернул бы дефект D6 (выбор по одному «сейчас», оценка — по другому), и на ошибку
        вызова должен указывать `mypy`, а не тест, который об этом не знает.
        """
        rows = await self._session.execute(_due_statement(limit=limit, now=now))
        ready: list[SubscriptionState] = []
        broken: list[BrokenSubscription] = []
        for subscription, passport in rows:
            try:
                ready.append(to_subscription_state(subscription, passport))
            except Exception as exc:
                # Не перечисляем «ожидаемые» типы: незнакомое значение может прийти любым
                # исключением разбора, и список ожиданий — это и есть дефект. Строка не
                # пропадает: она уходит наверх с причиной и попадёт в карантин.
                broken.append(
                    BrokenSubscription(
                        id=subscription.id,
                        user_id=subscription.user_id,
                        failed_streak=subscription.failed_streak,
                        error=describe_error(exc),
                    )
                )
        return DueSubscriptions(ready=ready, broken=broken)

    async def cancel_lapsed(self, *, now: datetime, grace: timedelta) -> int:
        """Отменить то, что стоит в очереди дольше льготы после окончания срока подписки.

        Найденное в оплаченный период доходит до клиента ещё `grace` после срока: он
        платил за находки, а не за момент, когда нотифаер до них добрался. Дальше строка
        становится `cancelled` — отменённой, а не потерянной: строка и запись
        `notifications` остаются, и карточка повторно не поставится. Продление срока
        внутри льготы возвращает подписку в право, и очередь доходит как ни в чём не
        бывало. Возвращает, сколько строк отменено.
        """
        cancelled = await self._session.execute(_cancel_statement(cutoff=now - grace))
        return len(cancelled.all())

    async def touch(self, subscription_id: int, *, now: datetime) -> None:
        """Отметить, что подписку брали в обход, не трогая состояние сбоев.

        Для подписки, которую пришлось пропустить (нет курса): она тоже «обойдена», иначе
        пропускаемые вечно стояли бы первыми и занимали всю порцию.
        """
        await self._session.execute(
            update(models.Subscription)
            .where(models.Subscription.id == subscription_id)
            .values(last_scanned_at=now)
            .execution_options(synchronize_session=False)
        )

    async def record_scan(self, subscription_id: int, *, now: datetime) -> None:
        """Проход по подписке удался: отметить обход и снять следы прежних сбоев."""
        await self._session.execute(
            update(models.Subscription)
            .where(models.Subscription.id == subscription_id)
            .values(last_scanned_at=now, failed_streak=0, last_error=None, quarantined_until=None)
            .execution_options(synchronize_session=False)
        )

    async def quarantine(
        self, subscription_id: int, *, now: datetime, streak: int, until: datetime, error: str
    ) -> None:
        """Изолировать подписку до `until`: её не выберут, соседей это не касается.

        `streak` приходит вызывающим, а не `failed_streak + 1` в SQL: пауза зависит от
        нового значения, и считать его в двух местах значило бы держать правило в двух
        местах. Строка заблокирована проходом (`claim_due`), так что значение не успеет
        измениться.
        """
        await self._session.execute(
            update(models.Subscription)
            .where(models.Subscription.id == subscription_id)
            .values(
                last_scanned_at=now,
                failed_streak=streak,
                last_error=error[:ERROR_LIMIT],
                quarantined_until=until,
            )
            .execution_options(synchronize_session=False)
        )

    async def ranked_slots(self, user_ids: Sequence[int], *, now: datetime) -> dict[int, list[int]]:
        """Слоты клиентов с правом и без ручной паузы — в порядке приоритета.

        Приоритета как колонки пока нет (его даст биллинг слотов), поэтому порядок — по `id`:
        более ранняя подписка старше. Условие то же `_servable`, что у выбора порции, иначе
        ранг считался бы среди слотов, которых монитор всё равно не возьмёт (карантинных,
        без текущего паспорта).
        """
        rows = await self._session.execute(_ranked_statement(user_ids, now=now))
        ranked: dict[int, list[int]] = {}
        for user_id, subscription_id in rows:
            ranked.setdefault(user_id, []).append(subscription_id)
        return ranked

    async def set_no_slot_since(self, subscription_id: int, since: datetime | None) -> None:
        """Отметить, с какого момента слот без права (`None` — снова работает)."""
        await self._session.execute(
            update(models.Subscription)
            .where(models.Subscription.id == subscription_id)
            .values(no_slot_since=since)
            .execution_options(synchronize_session=False)
        )

    async def record_overflow(
        self, subscription_id: int, overflow: Overflow, *, extra: int
    ) -> None:
        """Записать счёт «ещё N» за сутки и прибавить отброшенное к общему числу слота."""
        await self._session.execute(
            update(models.Subscription)
            .where(models.Subscription.id == subscription_id)
            .values(
                overflow_day=overflow.day,
                overflow_count=overflow.count,
                overflow_notified=overflow.notified,
                suppressed_total=models.Subscription.suppressed_total + extra,
            )
            .execution_options(synchronize_session=False)
        )
