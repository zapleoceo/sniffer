"""Форма запросов монитора без базы: что в них уходит и как они устроены.

Это не замена живой базе (`test_db_monitor.py`): текст оператора не доказывает, что Postgres
его выполнит так, как задумано. Но две вещи он доказывает дёшево и в каждом прогоне — что
в запрос дошло НУЖНОЕ ЗНАЧЕНИЕ (время прохода, а не часы машины) и что в нём стоит
ключевое условие (порядок обхода, фильтр карантина, предикат права). Локально, без
Docker, живые тесты пропускаются, и без этого файла такие правки не краснели бы нигде,
кроме CI.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sniffer.db.repositories.delivery import DeliveryRepository
from tests.monitor_support import Recorder, params_of

MOMENT = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


# ── часы (D6) ───────────────────────────────────────────────────────────────


async def test_enqueue_puts_the_given_moment_into_both_rows() -> None:
    """`created_at` — слот суток, `scheduled_at` — время доставки; оба от момента прохода."""
    session = Recorder(scalar=17)

    queued = await DeliveryRepository(session).enqueue(  # type: ignore[arg-type]
        subscription_id=1, user_id=2, listing_id=3, score=0.9, payload={"a": 1}, now=MOMENT
    )

    assert queued is True
    (insert,) = session.statements
    assert params_of(insert)["created_at"] == MOMENT
    (outbox,) = session.added
    assert outbox.scheduled_at == MOMENT


async def test_an_explicit_delivery_time_is_kept_apart_from_the_moment() -> None:
    """Дайджест уходит вечером, а слот суток занят уже сейчас: это два разных времени."""
    session = Recorder(scalar=17)
    evening = MOMENT + timedelta(hours=6)

    await DeliveryRepository(session).enqueue(  # type: ignore[arg-type]
        subscription_id=1,
        user_id=2,
        listing_id=3,
        score=0.9,
        payload={},
        scheduled_at=evening,
        now=MOMENT,
    )

    assert params_of(session.statements[0])["created_at"] == MOMENT
    assert session.added[0].scheduled_at == evening


async def test_a_duplicate_listing_adds_nothing_to_the_outbox() -> None:
    session = Recorder(scalar=None)

    queued = await DeliveryRepository(session).enqueue(  # type: ignore[arg-type]
        subscription_id=1, user_id=2, listing_id=3, score=0.9, payload={}, now=MOMENT
    )

    assert queued is False
    assert session.added == []


async def test_set_active_judges_the_term_by_the_given_moment() -> None:
    session = Recorder(scalar=5)

    changed = await DeliveryRepository(session).set_active(  # type: ignore[arg-type]
        user_id=1, passport_root=2, active=True, now=MOMENT
    )

    assert changed is True
    assert MOMENT in params_of(session.statements[0]).values()
