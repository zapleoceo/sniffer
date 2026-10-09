"""Адресный повтор отказа: журнал попыток и перенос ключа в очередь одной транзакцией.

Только SQL и сбор фактов. Решение «можно ли» принимает чистая `reject_retry.evaluate`:
здесь её вызывают под замком, а страница вызывает ту же функцию для кнопки, так что
второй копии правила нет.

Что делает `request()` и в каком порядке (всё в транзакции вызывающего, коммит за ним):

1. `pg_advisory_xact_lock` — один замок на ВСЕ повторы. Два одновременных запроса при
   9 использованных из 10 выстраиваются в очередь: второй считает окно уже после
   первого и получает отказ. `SELECT count(*)` без замка в READ COMMITTED пропустил бы
   обоих.
2. Повтор той же формы (`idempotency_key`) — возвращаем прежнюю попытку, ничего не пишем.
3. Ленивое закрытие завершённых попыток (`settle_finished`): joiner про эту таблицу не
   знает и не должен, поэтому «попытка закончилась» выводится из состояния очереди.
4. Блокировка строки отказа `FOR UPDATE`, факты, правило.
5. Разрешено → вставка попытки (со снимком отказа), удаление строки из `chat_rejects` и
   вставка в `chat_candidates`. Любое исключение откатывает всё: ключ не может оказаться
   одновременно нигде или в двух местах.

Строка `chat_rejects` удаляется, а не помечается — это сознательный выбор. Помеченная
осталась бы «отклонённой», и новый отказ того же ключа (joiner пишет его через
`ON CONFLICT DO NOTHING`) не записался бы: время осталось бы старым, а cooldown и
страница врали бы. Аудит при этом сохранён снимком в `chat_reject_retries`.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime

from sqlalchemy import case, delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert

from sniffer.db import models
from sniffer.db.repositories.base import Repository
from sniffer.domain import reject_reasons, reject_retry
from sniffer.domain.reject_reasons import RejectClass
from sniffer.domain.reject_retry import (
    RetryCode,
    RetryDecision,
    RetryFacts,
    RetryOffer,
    RetryRecord,
    RetryResult,
    RetryStatus,
)

# Отдельный от JOIN_LOCK_KEY (5_021_260) замок: лимит повторов и лимит вступлений —
# разные счётчики, и вступление не должно ждать, пока кто-то оформляет повтор.
RETRY_LOCK_KEY = 5_021_261

# Повтор, выбранный владельцем руками, идёт впереди находок разведки (100) и позади
# курируемого списка (10/20/30). Лимиты joiner от приоритета не зависят.
RETRY_PRIORITY = 50
RETRY_FOUND_IN = "retry:dashboard"

# Ключ, как его пишет разведка: `@имя` либо `+приглашение`. Всё остальное — не наш ключ.
_USERNAME = re.compile(r"^@[A-Za-z0-9_]{1,64}$")
_INVITE = re.compile(r"^\+[A-Za-z0-9_-]{1,128}$")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

OUTCOME_REJECTED_AGAIN = "rejected_again"
OUTCOME_LEFT_QUEUE = "left_queue"


def valid_key(key: str) -> bool:
    return bool(_USERNAME.match(key) or _INVITE.match(key))


def valid_idempotency_key(token: str) -> bool:
    return bool(_IDEMPOTENCY.match(token))


class RejectRetryRepository(Repository):
    async def request(
        self,
        key: str,
        *,
        idempotency_key: str,
        requested_by: int,
        now: datetime,
    ) -> RetryResult:
        if not valid_key(key):
            return _refused(RetryCode.BAD_KEY, "это не ключ кандидата")
        if not valid_idempotency_key(idempotency_key):
            return _refused(RetryCode.BAD_KEY, "форма без корректного токена — откройте заново")

        await self._session.execute(select(func.pg_advisory_xact_lock(RETRY_LOCK_KEY)))

        replay = await self._by_idempotency(idempotency_key)
        if replay is not None:
            if replay.reject_key != key:
                return _refused(RetryCode.BAD_KEY, "токен формы относится к другому ключу")
            return RetryResult(
                RetryStatus.REPLAYED,
                RetryDecision(RetryCode.OK, "эта форма уже принята — вторая попытка не заведена"),
                replay,
            )

        await self.settle_finished(now)

        reject = await self._session.scalar(
            select(models.ChatReject).where(models.ChatReject.key == key).with_for_update()
        )
        if reject is None:
            if await self._has_active(key):
                return _refused(RetryCode.ALREADY_QUEUED, "этот ключ уже стоит в очереди повтора")
            return _refused(RetryCode.NOT_FOUND, "такого отказа нет")

        facts = await self._facts(key, reject.reason, now)
        decision = reject_retry.evaluate(facts)
        if not decision.allowed:
            return RetryResult(RetryStatus.REFUSED, decision)

        queued = await self._session.scalar(
            insert(models.ChatCandidate)
            .values(
                key=key,
                username=key[1:] if key.startswith("@") else None,
                invite_hash=key[1:] if key.startswith("+") else None,
                found_in=RETRY_FOUND_IN,
                priority=RETRY_PRIORITY,
            )
            .on_conflict_do_nothing(index_elements=[models.ChatCandidate.key])
            .returning(models.ChatCandidate.id)
        )
        if queued is None:
            return _refused(RetryCode.ALREADY_QUEUED, "ключ уже стоит в очереди кандидатов")

        row = models.ChatRejectRetry(
            reject_key=key,
            reject_reason=reject.reason,
            reject_rejected_at=reject.rejected_at,
            requested_at=now,
            requested_by=requested_by,
            idempotency_key=idempotency_key,
            status="active",
            next_retry_at=reject_retry.next_retry_at(now),
        )
        self._session.add(row)
        await self._session.execute(delete(models.ChatReject).where(models.ChatReject.key == key))
        await self._session.flush()
        return RetryResult(RetryStatus.CREATED, decision, _record(row))

    async def settle_finished(self, now: datetime) -> int:
        """Закрыть попытки, ключи которых очередь уже отпустила.

        Исход выводится из состояния: появилась новая запись об отказе после запроса —
        `rejected_again`; иначе кандидата из очереди сняли вступлением или руками —
        `left_queue`. Joiner про этот журнал не знает, и учить его не нужно.
        """
        rejected_again = exists().where(
            models.ChatReject.key == models.ChatRejectRetry.reject_key,
            models.ChatReject.rejected_at >= models.ChatRejectRetry.requested_at,
        )
        in_queue = exists().where(models.ChatCandidate.key == models.ChatRejectRetry.reject_key)
        result = await self._session.execute(
            update(models.ChatRejectRetry)
            .where(models.ChatRejectRetry.status == "active", ~in_queue)
            .values(
                status="done",
                settled_at=now,
                outcome=case((rejected_again, OUTCOME_REJECTED_AGAIN), else_=OUTCOME_LEFT_QUEUE),
            )
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def offers(
        self, rejects: list[tuple[str, str]], now: datetime, blocked_until: datetime | None
    ) -> dict[str, RetryOffer]:
        """Решение правила для каждого отказа: то же, что применит `request()`."""
        used, oldest = await self._window(now)
        out: dict[str, RetryOffer] = {}
        for key, reason in rejects:
            if reject_reasons.classify(reason) is not RejectClass.TEMPORARY:
                continue
            total, last = await self._history(key)
            facts = RetryFacts(
                reason=reason,
                now=now,
                attempts_total=total,
                last_requested_at=last,
                used_in_window=used,
                oldest_in_window=oldest,
                blocked_until=blocked_until,
            )
            out[key] = RetryOffer(reject_retry.evaluate(facts), attempts_used=total)
        return out

    async def used_in_window(self, now: datetime) -> int:
        return (await self._window(now))[0]

    async def recent(self, *, limit: int = 20) -> list[RetryRecord]:
        """Журнал для страницы. Чтение чистое: статус выводится, а не дописывается.

        Закрывает попытки только `request()` (запись в GET — лишний повод для гонок);
        здесь активная попытка, ключа которой уже нет в очереди, показывается как
        завершённая с тем же исходом, какой запишет `settle_finished`.
        """
        rows = list(
            await self._session.scalars(
                select(models.ChatRejectRetry)
                .order_by(models.ChatRejectRetry.requested_at.desc())
                .limit(limit)
            )
        )
        active = [row.reject_key for row in rows if row.status == "active"]
        queued = set(
            await self._session.scalars(
                select(models.ChatCandidate.key).where(models.ChatCandidate.key.in_(active))
            )
        )
        out = []
        for row in rows:
            record = _record(row)
            if row.status == "active" and row.reject_key not in queued:
                again = await self._session.scalar(
                    select(models.ChatReject.key).where(
                        models.ChatReject.key == row.reject_key,
                        models.ChatReject.rejected_at >= row.requested_at,
                    )
                )
                record = replace(
                    record,
                    status="done",
                    outcome=OUTCOME_REJECTED_AGAIN if again else OUTCOME_LEFT_QUEUE,
                )
            out.append(record)
        return out

    async def _facts(self, key: str, reason: str, now: datetime) -> RetryFacts:
        used, oldest = await self._window(now)
        total, last = await self._history(key)
        blocked = await self._session.scalar(
            select(func.max(models.ChatJoinEvent.blocked_until)).where(
                models.ChatJoinEvent.blocked_until > now
            )
        )
        return RetryFacts(
            reason=reason,
            now=now,
            attempts_total=total,
            last_requested_at=last,
            used_in_window=used,
            oldest_in_window=oldest,
            blocked_until=blocked,
        )

    async def _window(self, now: datetime) -> tuple[int, datetime | None]:
        since = now - reject_retry.RETRY_WINDOW
        row = (
            await self._session.execute(
                select(
                    func.count(models.ChatRejectRetry.id),
                    func.min(models.ChatRejectRetry.requested_at),
                ).where(models.ChatRejectRetry.requested_at > since)
            )
        ).one()
        return int(row[0]), row[1]

    async def _history(self, key: str) -> tuple[int, datetime | None]:
        row = (
            await self._session.execute(
                select(
                    func.count(models.ChatRejectRetry.id),
                    func.max(models.ChatRejectRetry.requested_at),
                ).where(models.ChatRejectRetry.reject_key == key)
            )
        ).one()
        return int(row[0]), row[1]

    async def _by_idempotency(self, token: str) -> RetryRecord | None:
        row = await self._session.scalar(
            select(models.ChatRejectRetry).where(models.ChatRejectRetry.idempotency_key == token)
        )
        return _record(row) if row is not None else None

    async def _has_active(self, key: str) -> bool:
        return bool(
            await self._session.scalar(
                select(models.ChatRejectRetry.id)
                .where(
                    models.ChatRejectRetry.reject_key == key,
                    models.ChatRejectRetry.status == "active",
                )
                .limit(1)
            )
        )


def _refused(code: RetryCode, message: str) -> RetryResult:
    return RetryResult(RetryStatus.REFUSED, RetryDecision(code, message))


def _record(row: models.ChatRejectRetry) -> RetryRecord:
    return RetryRecord(
        id=row.id,
        reject_key=row.reject_key,
        reject_reason=row.reject_reason,
        reject_rejected_at=row.reject_rejected_at,
        requested_at=row.requested_at,
        requested_by=row.requested_by,
        status=row.status,
        outcome=row.outcome,
        next_retry_at=row.next_retry_at,
    )
