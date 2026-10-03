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

from sniffer.db import models
from sniffer.db.repositories.delivery import DeliveryRepository
from sniffer.db.repositories.monitors import ERROR_LIMIT, MonitorRepository, _due_statement
from tests.monitor_support import Recorder, params_of, sql_of

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
    # Момент прохода — и слот суток (`created_at`), и срок для проверки права: два параметра.
    assert list(params_of(insert).values()).count(MOMENT) == 2
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

    values = list(params_of(session.statements[0]).values())
    assert values.count(MOMENT) == 2, "слот суток занят в момент прохода"
    assert evening not in values, "вечернее время — только у письма, а не у записи о слоте"
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


# ── порция обхода: порядок, блокировка, карантин, право (D4, D5) ────────────

DAY = timedelta(days=1)


def _flat(statement: object) -> str:
    return " ".join(sql_of(statement).split())


def test_the_claim_goes_round_the_least_recently_scanned_first() -> None:
    """Прежний `ORDER BY id LIMIT 50` навсегда прятал 51-ю подписку (D4)."""
    sql = _flat(_due_statement(limit=50, now=MOMENT))

    assert "ORDER BY subscriptions.last_scanned_at ASC NULLS FIRST, subscriptions.id" in sql


def test_the_claim_locks_its_rows_and_skips_the_ones_a_neighbour_holds() -> None:
    sql = _flat(_due_statement(limit=50, now=MOMENT))

    assert sql.endswith("FOR UPDATE OF subscriptions SKIP LOCKED")


def test_the_claim_takes_exactly_the_batch_it_was_given() -> None:
    assert 7 in params_of(_due_statement(limit=7, now=MOMENT)).values()


def test_the_claim_leaves_a_quarantined_row_alone_until_its_time() -> None:
    sql = _flat(_due_statement(limit=50, now=MOMENT))
    params = params_of(_due_statement(limit=50, now=MOMENT))

    assert (
        "(subscriptions.quarantined_until IS NULL OR subscriptions.quarantined_until <= " in sql
    ), "срок карантина включительно: ровно в `quarantined_until` подписка снова в обходе"
    assert params["quarantined_until_1"] == MOMENT


def test_the_claim_asks_for_the_right_to_receive_at_the_given_moment() -> None:
    statement = _due_statement(limit=50, now=MOMENT)
    sql = _flat(statement)

    assert "subscriptions.is_active IS true" in sql
    assert "(subscriptions.expires_at IS NULL OR subscriptions.expires_at > " in sql
    assert params_of(statement)["expires_at_1"] == MOMENT


def test_the_claim_follows_the_current_version_of_the_chain() -> None:
    sql = _flat(_due_statement(limit=50, now=MOMENT))

    assert "coalesce(passports.root_id, passports.id) = subscriptions.passport_root" in sql
    assert "passports.is_current IS true" in sql


# ── больная строка не уносит остальных (D5) ─────────────────────────────────


def _subscription_row(number: int, *, streak: int = 0) -> models.Subscription:
    return models.Subscription(
        id=number,
        user_id=100 + number,
        passport_root=200 + number,
        is_active=True,
        mode="instant",
        max_per_day=5,
        quiet_from=None,
        quiet_to=None,
        sent_today=0,
        since_listing_id=0,
        scan_listing_id=0,
        expires_at=None,
        charge_id=None,
        last_scanned_at=None,
        failed_streak=streak,
        last_error=None,
        quarantined_until=None,
        day_bucket=None,
        created_at=MOMENT,
    )


def _passport_row(number: int, *, category: str | None = "motorbike") -> models.Passport:
    return models.Passport(
        id=200 + number,
        root_id=None,
        version=1,
        user_id=100 + number,
        status="draft",
        intent="buy",
        category=category,
        city="nha_trang",
        districts=[],
        budget={},
        attributes={},
        must_have=[],
        deal_breakers=[],
        timeframe_from=None,
        timeframe_to=None,
        raw_query="ищу скутер",
        confidence=0.0,
        missing_fields=[],
        created_at=MOMENT,
        is_current=True,
        last_used_at=None,
    )


async def test_one_unreadable_passport_does_not_take_the_whole_batch_down() -> None:
    """Незнакомое значение перечисления падает при разборе — раньше вместе со всей пачкой.

    Реалистичный сценарий: в код добавили значение `Category`, строки с ним легли в базу,
    код откатили (`git revert` — штатный откат). Разбор шёл списком, и воркер ходил по кругу
    «упал — перезапустили» вместе со всей воронкой (D5).
    """
    rows = [
        (_subscription_row(1), _passport_row(1)),
        (_subscription_row(2, streak=3), _passport_row(2, category="spaceship")),
        (_subscription_row(3), _passport_row(3)),
    ]
    session = Recorder(rows=rows)

    due = await MonitorRepository(session).claim_due(limit=50, now=MOMENT)  # type: ignore[arg-type]

    assert [state.id for state in due.ready] == [1, 3]
    (sick,) = due.broken
    assert (sick.id, sick.user_id, sick.failed_streak) == (2, 102, 3)
    assert "ValueError" in sick.error and "spaceship" in sick.error


async def test_the_reason_of_a_broken_row_is_short_enough_for_its_column() -> None:
    rows = [(_subscription_row(1), _passport_row(1, category="x" * 5_000))]

    due = await MonitorRepository(Recorder(rows=rows)).claim_due(limit=1, now=MOMENT)  # type: ignore[arg-type]

    assert len(due.broken[0].error) <= ERROR_LIMIT


async def test_a_readable_row_carries_its_failure_streak_to_the_pass() -> None:
    session = Recorder(rows=[(_subscription_row(1, streak=4), _passport_row(1))])

    due = await MonitorRepository(session).claim_due(limit=1, now=MOMENT)  # type: ignore[arg-type]

    assert due.ready[0].failed_streak == 4 and due.broken == []


# ── отметки обхода ──────────────────────────────────────────────────────────


async def test_a_successful_scan_is_stamped_and_wipes_the_traces_of_failures() -> None:
    session = Recorder()

    await MonitorRepository(session).record_scan(5, now=MOMENT)  # type: ignore[arg-type]

    params = params_of(session.statements[0])
    assert params["last_scanned_at"] == MOMENT
    assert params["failed_streak"] == 0
    assert params["last_error"] is None and params["quarantined_until"] is None


async def test_a_visit_without_a_scan_only_moves_the_slot_in_the_rotation() -> None:
    """`touch` для подписки, которую пропустили: сбои она не «лечит», ничего не сбрасывает."""
    session = Recorder()

    await MonitorRepository(session).touch(5, now=MOMENT)  # type: ignore[arg-type]

    assert set(params_of(session.statements[0])) - {"id_1"} == {"last_scanned_at"}


async def test_quarantine_writes_the_streak_the_reason_and_the_return_time() -> None:
    session = Recorder()
    until = MOMENT + timedelta(minutes=20)

    await MonitorRepository(session).quarantine(  # type: ignore[arg-type]
        5, now=MOMENT, streak=3, until=until, error="ValueError: плохой паспорт"
    )

    params = params_of(session.statements[0])
    assert params["failed_streak"] == 3
    assert params["quarantined_until"] == until
    assert params["last_error"] == "ValueError: плохой паспорт"
    assert params["last_scanned_at"] == MOMENT, "больную подписку тоже отправляют в конец обхода"


async def test_quarantine_cuts_an_overlong_reason() -> None:
    session = Recorder()

    await MonitorRepository(session).quarantine(  # type: ignore[arg-type]
        5, now=MOMENT, streak=1, until=MOMENT, error="я" * 10_000
    )

    assert len(params_of(session.statements[0])["last_error"]) == ERROR_LIMIT


# ── право на доставку: постановка и отмена (D7) ─────────────────────────────

ENTITLED = (
    "subscriptions.is_active IS true AND "
    "(subscriptions.expires_at IS NULL OR subscriptions.expires_at > "
)


async def test_enqueue_checks_the_right_to_receive_inside_the_insert() -> None:
    """Проверка в самой вставке: между «проверил» и «вставил» подписка успела бы истечь."""
    session = Recorder(scalar=17)

    await DeliveryRepository(session).enqueue(  # type: ignore[arg-type]
        subscription_id=1, user_id=2, listing_id=3, score=0.9, payload={}, now=MOMENT
    )

    (insert,) = session.statements
    sql = _flat(insert)
    assert sql.startswith(
        "INSERT INTO notifications (subscription_id, listing_id, score, created_at) SELECT "
    )
    assert "WHERE EXISTS (SELECT * FROM subscriptions WHERE subscriptions.id = " in sql
    assert ENTITLED in sql
    assert "ON CONFLICT (subscription_id, listing_id) DO NOTHING RETURNING notifications.id" in sql
    assert params_of(insert)["expires_at_1"] == MOMENT, "срок судят по моменту прохода"
    # Что именно вставляется и в каком порядке: подписка, карточка, оценка, слот суток — и
    # те же подписка и момент в проверке права. Перепутанные идентификаторы лежали бы в
    # чужой строке, а не падали бы ошибкой.
    assert list(params_of(insert).values()) == [1, 3, 0.9, MOMENT, 1, MOMENT]


async def test_the_claim_and_the_enqueue_ask_one_and_the_same_predicate() -> None:
    """Копия условия разъезжается тихо: выбор отсёк просроченную, а постановка поставила бы."""
    session = Recorder(scalar=17)
    await DeliveryRepository(session).enqueue(  # type: ignore[arg-type]
        subscription_id=1, user_id=2, listing_id=3, score=0.9, payload={}, now=MOMENT
    )

    assert ENTITLED in _flat(_due_statement(limit=1, now=MOMENT))
    assert ENTITLED in _flat(session.statements[0])


async def test_a_lapsed_subscription_adds_nothing_to_the_outbox() -> None:
    """Вставка не вернула строку — ни записи, ни письма в очереди."""
    session = Recorder(scalar=None)

    queued = await DeliveryRepository(session).enqueue(  # type: ignore[arg-type]
        subscription_id=1, user_id=2, listing_id=3, score=0.9, payload={}, now=MOMENT
    )

    assert queued is False and session.added == []


async def test_cancel_lapsed_cancels_pending_rows_of_subscriptions_expired_before_cutoff() -> None:
    session = Recorder(rows=[(1,), (2,)])

    cancelled = await MonitorRepository(session).cancel_lapsed(  # type: ignore[arg-type]
        now=MOMENT, grace=timedelta(hours=6)
    )

    assert cancelled == 2
    (statement,) = session.statements
    sql = _flat(statement)
    assert sql.startswith("UPDATE outbox SET status=")
    assert "outbox.subscription_id IN (SELECT subscriptions.id FROM subscriptions WHERE " in sql
    assert "subscriptions.expires_at < " in sql, "ровно в границу льготы доставка ещё идёт"
    params = params_of(statement)
    assert params["status"] == "cancelled"
    assert params["status_1"] == "pending", "отменяем только ждущее: отправленное не трогаем"
    assert params["expires_at_1"] == MOMENT - timedelta(hours=6)


async def test_the_grace_moves_the_cutoff_back_by_exactly_its_length() -> None:
    session = Recorder()

    await MonitorRepository(session).cancel_lapsed(  # type: ignore[arg-type]
        now=MOMENT, grace=timedelta(minutes=90)
    )

    assert params_of(session.statements[0])["expires_at_1"] == MOMENT - timedelta(minutes=90)
