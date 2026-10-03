"""Отложенные ответы с квотой: у каждого получателя свой остаток, общий анализ.

Те же правила, что у диалога, и одна точка `admit`: анализ каталога делается один раз
на всех подписчиков задачи, а то, сколько карточек каждому можно показать, — у
каждого своё. Подделки те же, что в `test_agent_followup.py`: репозиторий задач и
сессии, плюс журнал показов в памяти.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.agent_app import collector, followup, followup_quota
from sniffer.agent_app.main import CatalogAnswer
from sniffer.bot import wording_plan
from sniffer.bot.quota import Account, QuotaService
from sniffer.db.repositories.collection_tasks import CollectionLease, CollectionRecipient
from sniffer.domain.quota import Channel
from sniffer.notifier.delivery import render
from sniffer.simulation.ledger import MemoryLedger
from sniffer.sources.base import RawItem
from tests.quota_support import Clock

T0 = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)
RENEWS = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
LEASE = CollectionLease(7, "lease", {}, 1, datetime.now(UTC) + timedelta(minutes=2))
OWNER_TG = 169510539
TG_OF = {156: 42, 157: 43, 158: OWNER_TG}


def recipients(*ids: int) -> list[CollectionRecipient]:
    return [CollectionRecipient(user_id, 21, 1) for user_id in ids]


def found_items(count: int) -> list[RawItem]:
    return [
        RawItem(
            source="archive",
            external_id=f"ext-{number}",
            url=f"https://t.me/example/{number}",
            title=f"Honda rental {number}",
            price_raw="3 млн/месяц",
        )
        for number in range(1, count + 1)
    ]


class World:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, who: list[CollectionRecipient]) -> None:
        self.ledger = MemoryLedger()
        self.quota = QuotaService(self.ledger, clock=Clock(T0), owner_tg_id=OWNER_TG)
        self.repo = SimpleNamespace(
            pending_recipients=AsyncMock(return_value=who),
            queue_reply=AsyncMock(return_value=True),
        )
        session = SimpleNamespace(commit=AsyncMock())

        @asynccontextmanager
        async def sessions() -> AsyncIterator[AsyncSession]:
            yield cast(AsyncSession, session)

        async def user(user_id: int) -> Any:
            return SimpleNamespace(tg_user_id=TG_OF[user_id])

        monkeypatch.setattr(followup, "session_scope", sessions)
        monkeypatch.setattr(followup, "CollectionTaskRepository", lambda _: self.repo)
        monkeypatch.setattr(followup_quota, "UserRepository", lambda _: SimpleNamespace(get=user))
        self.items = found_items(5)
        for index, item in enumerate(self.items):
            self.ledger.known[(item.source, item.external_id)] = 1000 + index
        monkeypatch.setattr(
            followup, "search_request", AsyncMock(return_value=CatalogAnswer(self.items))
        )

    def payload(self, call: int) -> dict[str, Any]:
        return cast(dict[str, Any], self.repo.queue_reply.await_args_list[call].args[3])

    async def spend(self, user_id: int, count: int) -> None:
        """Прошлые показы этого человека: так тратится его бесплатный остаток."""
        who = Account(user_id=user_id, tg_user_id=TG_OF[user_id])
        await self.quota.confirm(await self.quota.admit(who, list(range(1, count + 1))))


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    return World(monkeypatch, recipients(156, 157))


async def test_each_recipient_gets_the_cards_their_own_allowance_lets_them_see(
    world: World,
) -> None:
    await world.spend(156, 8)

    assert await followup.queue_answers(LEASE, quota=world.quota) == 2

    poor, rich = render(world.payload(0)), render(world.payload(1))
    assert poor.count("Honda rental") == 2 and "Бесплатно осталось 0 из 10" in poor
    assert "Ещё 3 подходящих варианта — по подписке 10 ⭐/мес." in poor
    assert rich.count("Honda rental") == 5 and "Бесплатно осталось 5 из 10" in rich
    assert "Ещё" not in rich


async def test_a_recipient_with_nothing_left_gets_the_offer_instead_of_cards(
    world: World,
) -> None:
    await world.spend(156, 10)

    await followup.queue_answers(LEASE, quota=world.quota)

    message = render(world.payload(0))
    first_line = wording_plan.exhausted_offer(total=5, renews=RENEWS).splitlines()[0]
    assert "Honda rental" not in message
    assert "Обновление каталога завершено." in message and first_line in message


async def test_the_offer_is_not_repeated_to_the_same_person_within_a_day(world: World) -> None:
    await world.spend(156, 10)
    await world.ledger.claim_offer(156, T0, timedelta(days=1))  # сегодня уже предлагали

    await followup.queue_answers(LEASE, quota=world.quota)

    message = render(world.payload(0))
    assert "Honda rental" not in message and "Что можно сделать сейчас" not in message
    assert "Ваши поиски сохранены" in message


async def test_the_reservation_is_confirmed_when_the_reply_entered_the_outbox(
    world: World,
) -> None:
    await followup.queue_answers(LEASE, quota=world.quota)

    rows = world.ledger.rows(156)
    assert len(rows) == 5 and all(view.delivered_at for view in rows)
    assert all(view.passport_root == 21 for view in rows), "ветка получателя — корень паспорта"
    assert {view.channel for view in rows} == {Channel.DEFERRED}


async def test_a_reply_that_was_not_queued_returns_the_slots(world: World) -> None:
    world.repo.queue_reply.return_value = False

    assert await followup.queue_answers(LEASE, quota=world.quota) == 0

    assert world.ledger.rows(156) == [] and world.ledger.rows(157) == []


async def test_a_failing_outbox_returns_the_slots_and_the_error_goes_on(world: World) -> None:
    world.repo.queue_reply.side_effect = ConnectionError("база недоступна")

    with pytest.raises(ConnectionError):
        await followup.queue_answers(LEASE, quota=world.quota)

    assert world.ledger.rows(156) == []


async def test_the_owner_is_not_limited_and_sees_no_balance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(monkeypatch, recipients(158))

    await followup.queue_answers(LEASE, quota=world.quota)

    message = render(world.payload(0))
    assert message.count("Honda rental") == 5 and "осталось" not in message.lower()


async def test_without_a_quota_every_recipient_gets_the_same_unmetered_answer(
    world: World,
) -> None:
    await followup.queue_answers(LEASE)

    message = render(world.payload(0))
    assert message.count("Honda rental") == 5 and "осталось" not in message.lower()
    assert world.ledger.rows(156) == []


async def test_the_collector_hands_its_quota_to_the_reply_and_defaults_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = cast(QuotaService, object())
    answered, failed = AsyncMock(return_value=1), AsyncMock(return_value=1)
    monkeypatch.setattr(collector, "queue_answers", answered)
    monkeypatch.setattr(collector, "queue_failure_answers", failed)

    metered = collector.Collector(quota=sentinel)
    await metered._reply(LEASE)
    await metered._failure_reply(LEASE)
    answered.assert_awaited_once_with(LEASE, quota=sentinel)
    failed.assert_awaited_once_with(LEASE, quota=sentinel)

    answered.reset_mock()
    await collector.Collector()._reply(LEASE)
    answered.assert_awaited_once_with(LEASE)
