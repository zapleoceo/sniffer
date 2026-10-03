"""Монитор подписок в базе: кого взять в обход, как отметить обход, кого изолировать.

Отдельно от `delivery.py` по причине, а не ради счётчика строк: там живут очередь доставки и
платежи, а здесь — состояние самого обхода (ротация и карантин). Второй ответственностью
в одном файле было бы то, что менять их приходится по разным поводам.

Три свойства, ради которых модуль есть:

* **Обход по кругу.** Порядок — «кого не смотрели дольше всех, первым», а не по `id`:
  прежний `ORDER BY id LIMIT 50` навсегда прятал 51-ю подписку (D4).
* **Больная строка не уносит остальных.** Паспорт с незнакомым значением перечисления
  падает при разборе; пачка, разбираемая списком, падала целиком, и воркер вместе с ней
  (D5). Здесь каждая строка разбирается отдельно, а больная возвращается с причиной.
* **Карантин по времени.** Сбойная подписка не выбирается до `quarantined_until`, потом
  выбирается снова сама: чинить её руками ради возвращения в обход не нужно.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, func, or_, select, update

from sniffer.db import models
from sniffer.db.mappers import to_subscription_state
from sniffer.db.repositories.base import Repository
from sniffer.db.repositories.delivery import entitled
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


def _due_statement(
    *, limit: int, now: datetime
) -> Select[tuple[models.Subscription, models.Passport]]:
    # Подписка хранит корень цепочки, а не версию: клиент правит запрос, и подписка обязана
    # следовать за правкой, а не застывать на версии, при которой её создали. Отсюда join
    # по `COALESCE(root_id, id)` и условие `is_current`.
    chain = func.coalesce(models.Passport.root_id, models.Passport.id)
    return (
        select(models.Subscription, models.Passport)
        .join(models.Passport, chain == models.Subscription.passport_root)
        .where(
            models.Passport.is_current.is_(True),
            entitled(now),
            # Карантин кончается сам: строка с истёкшим сроком снова в обходе.
            or_(
                models.Subscription.quarantined_until.is_(None),
                models.Subscription.quarantined_until <= now,
            ),
        )
        # NULLS FIRST: новая подписка (ни разу не смотрели) идёт раньше всех, а дальше —
        # по давности обхода. `id` разводит равные: порядок должен быть полным, иначе
        # две подписки с одним временем могли бы меняться местами от прохода к проходу.
        .order_by(models.Subscription.last_scanned_at.asc().nulls_first(), models.Subscription.id)
        .with_for_update(of=models.Subscription, skip_locked=True)
        .limit(limit)
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

    async def touch(self, subscription_id: int, *, now: datetime) -> None:
        """Отметить, что подписку брали в обход, не трогая состояние сбоев.

        Для подписки, которую пришлось пропустить (нет курса): она тоже «обойдена», иначе
        пропускаемые вечно стояли бы первыми и занимали всю порцию.
        """
        await self._session.execute(
            update(models.Subscription)
            .where(models.Subscription.id == subscription_id)
            .values(last_scanned_at=now)
        )

    async def record_scan(self, subscription_id: int, *, now: datetime) -> None:
        """Проход по подписке удался: отметить обход и снять следы прежних сбоев."""
        await self._session.execute(
            update(models.Subscription)
            .where(models.Subscription.id == subscription_id)
            .values(last_scanned_at=now, failed_streak=0, last_error=None, quarantined_until=None)
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
        )
