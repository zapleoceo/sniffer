"""Репозитории наблюдаемости на живом Postgres.

Пропускаются, пока не задан `TEST_DATABASE_URL` (см. `conftest.py`): проверять
частичный уникальный индекс, `JSONB` и `NUMERIC(12,6)` не на Postgres
бессмысленно — именно эти места и ломаются, а на подделке они зелёные всегда.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer import crypto
from sniffer.config import reload_settings
from sniffer.db import models
from sniffer.db.repositories import (
    BrokerCallRepository,
    ClientRequestRepository,
    DialogRepository,
    StatsRepository,
    TelegramSessionRepository,
    UserRepository,
)
from sniffer.domain.records import (
    REQUEST_DONE,
    REQUEST_FAILED,
    BrokerCall,
    User,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL не задан: живого Postgres нет",
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
ENCRYPTION_KEY = "encryption-key-длинный-и-случайный-32+"


@pytest.fixture
def encryption(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", ENCRYPTION_KEY)
    reload_settings()
    yield
    reload_settings()


async def _user(session: AsyncSession, tg_user_id: int = 169510539) -> User:
    user = await UserRepository(session).get_or_create(tg_user_id, username="client")
    assert user.id is not None
    return user


# ── запросы клиентов ────────────────────────────────────────────────────────


async def test_request_opens_running_and_closes_done(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    repo = ClientRequestRepository(db_session)

    opened = await repo.open(user.id or 0, "ищу скутер в Нячанге")
    assert opened.status == "running"
    assert opened.duration_ms is None

    closed = await repo.finish(
        opened.id,
        result_count=5,
        stages={"intake_ms": 1200, "plan_ms": 900, "search_ms": 8000},
        plan_fallback=True,
        sources=["chotot"],
    )
    await db_session.commit()

    assert closed is not None
    assert closed.status == REQUEST_DONE
    # Этапы обязаны вернуться числами: JSONB отдаёт то, что положили, и строка
    # здесь означала бы сумму «1200900» вместо суммы времени.
    assert closed.stages == {"intake_ms": 1200, "plan_ms": 900, "search_ms": 8000}
    assert closed.sources == ["chotot"], "TEXT[] должен вернуться списком"
    assert closed.duration_ms is not None and closed.duration_ms >= 0


async def test_failed_request_stays_in_the_log(db_session: AsyncSession) -> None:
    """Упавший запрос обязан остаться: иначе в логе видно только удачные."""
    user = await _user(db_session)
    repo = ClientRequestRepository(db_session)
    opened = await repo.open(user.id or 0, "ищу вертолёт")

    closed = await repo.finish(opened.id, stages={}, error="RuntimeError: источник отдал не то")
    await db_session.commit()

    assert closed is not None
    assert closed.status == REQUEST_FAILED
    assert "RuntimeError" in (closed.error or "")
    assert [row.id for row in await repo.recent()] == [opened.id]


async def test_finishing_a_missing_request_is_not_an_error(db_session: AsyncSession) -> None:
    assert await ClientRequestRepository(db_session).finish(999999, stages={}) is None


async def test_request_totals_count_failures_and_fallbacks(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    repo = ClientRequestRepository(db_session)
    first = await repo.open(user.id or 0, "раз")
    second = await repo.open(user.id or 0, "два")
    await repo.finish(first.id, stages={}, plan_fallback=True)
    await repo.finish(second.id, stages={}, error="упало")
    await db_session.commit()

    totals = await repo.totals()

    assert totals["requests"] == 2
    assert totals["failed"] == 1
    assert totals["fallbacks"] == 1
    assert await repo.counts_by_user() == {user.id: 2}


# ── переписка ───────────────────────────────────────────────────────────────


async def test_dialog_keeps_both_sides_in_order(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    request = await ClientRequestRepository(db_session).open(user.id or 0, "ищу скутер")
    repo = DialogRepository(db_session)

    await repo.log_incoming(user.id or 0, "ищу скутер", request_id=request.id)
    await repo.log_outgoing(user.id or 0, "Понял, ищу.", request_id=request.id)
    await db_session.commit()

    turn = await repo.by_request(request.id)
    assert [(message.direction, message.text) for message in turn] == [
        ("in", "ищу скутер"),
        ("out", "Понял, ищу."),
    ]
    # Лента клиента идёт свежим сверху — так её и читают.
    assert [message.direction for message in await repo.by_user(user.id or 0)] == ["out", "in"]


async def test_long_answer_is_truncated_not_refused(db_session: AsyncSession) -> None:
    """Карточки выдачи — простыня; таблица не должна расти от полных копий."""
    user = await _user(db_session)
    repo = DialogRepository(db_session)

    stored = await repo.log_outgoing(user.id or 0, "х" * 9000)
    await db_session.commit()

    assert len(stored.text) == 4000


# ── расходы ─────────────────────────────────────────────────────────────────


def _call(request_id: int, broker_request_id: int | None, cost: str) -> BrokerCall:
    return BrokerCall(
        capability="structured",
        request_id=request_id,
        broker_request_id=broker_request_id,
        provider="groq",
        model="llama-3.3-70b",
        tokens_in=120,
        tokens_out=45,
        cost_usd=Decimal(cost),
        latency_ms=870,
    )


async def test_cost_is_linked_to_the_request_by_key(db_session: AsyncSession) -> None:
    """Главное свойство: связь по ключу, а не по времени."""
    user = await _user(db_session)
    requests = ClientRequestRepository(db_session)
    first = await requests.open(user.id or 0, "про байк")
    second = await requests.open(user.id or 0, "про квартиру")
    repo = BrokerCallRepository(db_session)

    await repo.record(_call(first.id, 100, "0.000100"))
    await repo.record(_call(first.id, 101, "0.000200"))
    await repo.record(_call(second.id, 200, "0.001000"))
    await db_session.commit()

    costs = await repo.cost_by_request([first.id, second.id])

    assert costs[first.id] == (330, Decimal("0.000300"))
    assert costs[second.id] == (165, Decimal("0.001000"))


async def test_same_broker_request_is_recorded_once(db_session: AsyncSession) -> None:
    """Поллинг может вернуть завершённую задачу дважды — расход один."""
    user = await _user(db_session)
    request = await ClientRequestRepository(db_session).open(user.id or 0, "про байк")
    repo = BrokerCallRepository(db_session)

    assert await repo.record(_call(request.id, 4242, "0.000100")) is not None
    assert await repo.record(_call(request.id, 4242, "0.000100")) is None
    await db_session.commit()

    assert len(await repo.by_request(request.id)) == 1


async def test_calls_without_broker_id_do_not_collide(db_session: AsyncSession) -> None:
    """Частичный индекс: у вызова без ответа брокера ключа нет."""
    user = await _user(db_session)
    request = await ClientRequestRepository(db_session).open(user.id or 0, "про байк")
    repo = BrokerCallRepository(db_session)

    await repo.record(_call(request.id, None, "0"))
    await repo.record(_call(request.id, None, "0"))
    await db_session.commit()

    assert len(await repo.by_request(request.id)) == 2


async def test_cost_keeps_six_decimal_places(db_session: AsyncSession) -> None:
    """NUMERIC(12,6): доли цента не должны исчезать при округлении."""
    user = await _user(db_session)
    request = await ClientRequestRepository(db_session).open(user.id or 0, "про байк")
    repo = BrokerCallRepository(db_session)

    await repo.record(_call(request.id, 1, "0.000001"))
    await db_session.commit()

    assert (await repo.by_request(request.id))[0].cost_usd == Decimal("0.000001")
    assert await repo.cost_by_request([]) == {}


async def test_cost_totals_sum_everything(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    request = await ClientRequestRepository(db_session).open(user.id or 0, "про байк")
    repo = BrokerCallRepository(db_session)
    await repo.record(_call(request.id, 1, "0.000100"))
    await repo.record(_call(request.id, 2, "0.000200"))
    await db_session.commit()

    totals = await repo.totals()

    assert totals["calls"] == 2
    assert totals["tokens_in"] == 240
    assert totals["cost_usd"] == Decimal("0.000300")


# ── сессия юзербота ─────────────────────────────────────────────────────────


async def test_session_is_stored_encrypted(db_session: AsyncSession, encryption: None) -> None:
    """В колонке шифртекст: дамп базы не должен давать доступ к аккаунту."""
    secret = "1BQANOTEuMTA4LjU2LjEyOAG7VERYSECRETSESSION"
    repo = TelegramSessionRepository(db_session)

    state = await repo.save("+84900000000", secret)
    await db_session.commit()

    assert state.is_active
    # Доменная запись секрета не несёт — её отдают дашборду.
    assert secret not in str(state)
    assert await repo.active_session_string() == secret

    # Смотрим прямо в колонку через ORM-зеркало: правило «SQL только в db/»
    # распространяется и на тесты, поэтому без текстового запроса.
    row = await db_session.get(models.TelegramSession, state.id)
    assert row is not None
    assert secret not in row.session_enc
    assert row.session_enc.startswith(crypto.ENCRYPTED_PREFIX)


async def test_only_one_session_stays_active(db_session: AsyncSession, encryption: None) -> None:
    """Две активные сессии одного аккаунта дают AuthKeyDuplicated."""
    repo = TelegramSessionRepository(db_session)
    await repo.save("+84900000000", "первая")
    await repo.save("+84911111111", "вторая")
    await db_session.commit()

    states = await repo.states()

    assert [state.is_active for state in states] == [False, True]
    assert await repo.active_session_string() == "вторая"


async def test_broken_session_is_deactivated_with_a_reason(
    db_session: AsyncSession, encryption: None
) -> None:
    """Коллектор с отозванной сессией не выздоравливает — перебор чатов ведёт к бану."""
    repo = TelegramSessionRepository(db_session)
    await repo.save("+84900000000", "строка")
    await repo.mark_failed("+84900000000", "AuthKeyUnregistered")
    await db_session.commit()

    assert await repo.active_state() is None
    state = (await repo.states())[0]
    assert not state.is_active
    assert state.last_error == "AuthKeyUnregistered"
    assert state.last_error_at is not None


async def test_successful_reconnect_clears_the_error(
    db_session: AsyncSession, encryption: None
) -> None:
    repo = TelegramSessionRepository(db_session)
    await repo.save("+84900000000", "строка")
    await repo.mark_failed("+84900000000", "AuthKeyUnregistered")
    await repo.save("+84900000000", "новая строка")
    await repo.mark_ok("+84900000000")
    await db_session.commit()

    state = await repo.active_state()

    assert state is not None
    assert state.last_error is None
    assert state.last_ok_at is not None


async def test_no_session_reads_as_none(db_session: AsyncSession, encryption: None) -> None:
    repo = TelegramSessionRepository(db_session)

    assert await repo.active_state() is None
    assert await repo.active_session_string() is None


# ── сводка ──────────────────────────────────────────────────────────────────


async def test_summary_counts_on_an_empty_base(db_session: AsyncSession) -> None:
    """Пустая база — это нули, а не падение на None."""
    summary = await StatsRepository(db_session).summary()

    assert summary["users"] == 0
    assert summary["listings_fresh"] == 0
    assert summary["chats_active"] == 0
