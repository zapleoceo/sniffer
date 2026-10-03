"""Журнал показов: один набор сценариев на подделку и на Postgres.

Каждый сценарий — правило продукта, записанное как поведение `QuotaService` над
журналом: единица квоты, период-«годовщина», резерв и возврат, потолок на момент
выдачи. Прогоняется на `MemoryLedger` всегда и на `SqlLedger` — когда есть
`TEST_DATABASE_URL`. Одинаковый результат на обеих — и есть контракт: подделка,
которой верят без сверки, мерит саму себя.

Фиксированные моменты названы `T0` и далее, а не `NOW`: квотный SQL не сверяет
ничего с часами базы, «сейчас» ему всегда передают параметром, поэтому дата не
протухнет сама (чего боится `test_db_clock_rule.py`). Гонки и барьеры самой базы —
в `test_quota_db.py`.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from sniffer.bot.quota import Account
from sniffer.db import collection_models
from sniffer.domain.plans import FREE_CARDS_PER_PERIOD, PAID_CARDS_PER_PERIOD
from sniffer.domain.quota import RESERVATION_TTL, Admission, Channel, Standing
from tests.quota_support import (
    OWNER_TG,
    T0,
    Clock,
    Kit,
    MemoryKit,
    Slots,
    SqlKit,
    service,
    started,
)

assert collection_models  # метаданные сборщика регистрируются только импортом (см. conftest)

Scenario = Callable[[Kit], Awaitable[None]]
SCENARIOS: list[Scenario] = []


def scenario(function: Scenario) -> Scenario:
    """Каждый сценарий исполняется на обеих реализациях: забыть зарегистрировать нельзя."""
    SCENARIOS.append(function)
    return function


# ── единица квоты ───────────────────────────────────────────────────────────


@scenario
async def first_admission_sets_the_anchor_and_opens_period_zero(kit: Kit) -> None:
    user, who = await started(kit)
    ids = await kit.listings(3)
    quota = service(kit)
    assert await kit.anchor(user) is None

    admission = await quota.admit(who, ids)

    assert admission.granted == tuple(ids)
    assert (admission.remaining, admission.limit) == (7, FREE_CARDS_PER_PERIOD)
    assert admission.period_end == datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
    assert await kit.anchor(user) == T0
    assert await kit.period_count(user) == 1
    assert [row.delivered for row in await kit.rows(user)] == [False] * 3, "до отправки это резерв"
    await quota.confirm(admission)
    assert all(row.delivered for row in await kit.rows(user))


@scenario
async def a_repeat_in_the_same_period_is_free_and_counted_as_a_view(kit: Kit) -> None:
    user, who = await started(kit)
    ids = await kit.listings(4)
    quota = service(kit)
    await quota.confirm(await quota.admit(who, ids[:3]))

    second = await quota.admit(who, [ids[1], ids[2], ids[3]])

    assert second.repeated == (ids[1], ids[2])
    assert second.granted == (ids[3],)
    assert second.remaining == FREE_CARDS_PER_PERIOD - 4
    shown = {row.listing_id: row.times_shown for row in await kit.rows(user)}
    assert shown == {ids[0]: 1, ids[1]: 2, ids[2]: 2, ids[3]: 1}


@scenario
async def replaying_the_same_update_charges_nothing_twice(kit: Kit) -> None:
    """Telegram повторяет апдейт, если бот не ответил вовремя: списать дважды нельзя."""
    user, who = await started(kit)
    ids = await kit.listings(5)
    quota = service(kit)

    first = await quota.admit(who, ids)
    replay = await quota.admit(who, ids)

    assert first.granted == tuple(ids)
    assert replay.granted == () and replay.repeated == tuple(ids)
    assert replay.remaining == first.remaining == 5
    assert len(await kit.rows(user)) == 5


@scenario
async def the_same_card_twice_in_one_issue_is_one_card(kit: Kit) -> None:
    user, who = await started(kit)
    a, b = await kit.listings(2)

    admission = await service(kit).admit(who, [a, a, b, a])

    assert admission.granted == (a, b)
    assert len(await kit.rows(user)) == 2


@scenario
async def nothing_found_opens_no_period_and_writes_nothing(kit: Kit) -> None:
    """Период начинается с первой РЕАЛЬНО выданной карточки, а не с первого поиска."""
    user, who = await started(kit)

    admission = await service(kit).admit(who, [])

    assert admission == Admission()
    assert await kit.anchor(user) is None
    assert await kit.period_count(user) == 0


@scenario
async def two_accounts_do_not_share_a_period_or_a_card(kit: Kit) -> None:
    (first, one), (second, two) = await started(kit), await started(kit)
    ids = await kit.listings(3)
    quota = service(kit)

    a, b = await quota.admit(one, ids), await quota.admit(two, ids)

    assert a.granted == b.granted == tuple(ids)
    assert a.remaining == b.remaining == 7
    assert len(await kit.rows(first)) == len(await kit.rows(second)) == 3


# ── остаток и исчерпание ────────────────────────────────────────────────────


@scenario
async def a_partial_issue_gives_the_best_cards_up_to_the_remainder(kit: Kit) -> None:
    """Осталось 4, подходит 37: уходят четыре лучшие, остальные удержаны."""
    user, who = await started(kit)
    quota = service(kit)
    await quota.confirm(await quota.admit(who, await kit.listings(6)))
    found = await kit.listings(37)

    admission = await quota.admit(who, found)

    assert admission.granted == tuple(found[:4])
    assert admission.withheld == tuple(found[4:])
    assert admission.remaining == 0
    assert len(await kit.rows(user)) == FREE_CARDS_PER_PERIOD


@scenario
async def what_was_seen_stays_free_at_a_zero_remainder(kit: Kit) -> None:
    user, who = await started(kit)
    quota = service(kit)
    paid = await kit.listings(FREE_CARDS_PER_PERIOD)
    await quota.confirm(await quota.admit(who, paid))
    fresh = await kit.listings(2)

    admission = await quota.admit(who, [paid[0], fresh[0], paid[1], fresh[1]])

    assert admission.repeated == (paid[0], paid[1])
    assert admission.granted == ()
    assert admission.withheld == tuple(fresh)
    assert admission.remaining == 0
    assert len(await kit.rows(user)) == FREE_CARDS_PER_PERIOD


@scenario
async def a_new_period_makes_a_seen_card_paid_again(kit: Kit) -> None:
    user, who = await started(kit)
    ids = await kit.listings(FREE_CARDS_PER_PERIOD)
    quota = service(kit)
    await quota.confirm(await quota.admit(who, ids))

    later = await quota.admit(who, ids, now=datetime(2026, 11, 17, 9, 30, tzinfo=UTC))

    assert later.granted == tuple(ids), "ровно в границу начинается следующий период"
    assert later.period_end == datetime(2026, 12, 17, 9, 30, tzinfo=UTC)
    assert later.remaining == 0
    assert await kit.period_count(user) == 2


@scenario
async def a_boundary_inside_one_move_keeps_each_issue_in_its_own_period(kit: Kit) -> None:
    """Выдача на микросекунду до границы — в старом периоде, на границе — в новом.

    Подтверждение приходит уже после границы, но относится к периоду выдачи: его
    держит чек, а не пересчёт по часам.
    """
    user, who = await started(kit)
    before, after = await kit.listings(FREE_CARDS_PER_PERIOD), await kit.listings(3)
    quota = service(kit)
    edge = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)

    old = await quota.admit(who, before, now=T0)
    last = await quota.admit(who, after, now=edge - timedelta(microseconds=1))
    new = await quota.admit(who, after, now=edge)

    assert old.remaining == 0 and last.granted == () and last.withheld == tuple(after)
    assert new.granted == tuple(after) and new.remaining == FREE_CARDS_PER_PERIOD - 3
    assert new.period_end == datetime(2026, 12, 17, 9, 30, tzinfo=UTC)
    await service(kit, Clock(edge + timedelta(seconds=5))).confirm(old)
    delivered = {row.listing_id for row in await kit.rows(user) if row.delivered}
    assert delivered == set(before)


# ── границы периодов по календарю ───────────────────────────────────────────

MONTH_ENDS = [
    # якорь (UTC)                 конец первого периода (UTC)
    (datetime(2027, 1, 31, 10, tzinfo=UTC), datetime(2027, 2, 28, 10, tzinfo=UTC)),
    (datetime(2028, 1, 31, 10, tzinfo=UTC), datetime(2028, 2, 29, 10, tzinfo=UTC)),
    (datetime(2026, 8, 31, 10, tzinfo=UTC), datetime(2026, 9, 30, 10, tzinfo=UTC)),
    (datetime(2026, 12, 15, 23, 59, 59, tzinfo=UTC), datetime(2027, 1, 15, 23, 59, 59, tzinfo=UTC)),
    (datetime(2026, 12, 31, 10, tzinfo=UTC), datetime(2027, 1, 31, 10, tzinfo=UTC)),
    # 31 октября 02:00 по Хошимину — это 30-е 19:00 UTC; «месяц» считается по местным числам
    (datetime(2026, 10, 30, 19, tzinfo=UTC), datetime(2026, 11, 29, 19, tzinfo=UTC)),
]


@scenario
async def period_ends_follow_the_vietnamese_calendar_from_the_anchor(kit: Kit) -> None:
    for anchor, expected_end in MONTH_ENDS:
        user, who = await started(kit)
        ids = await kit.listings(2)
        quota = service(kit)

        first = await quota.admit(who, ids[:1], now=anchor)
        inside = await quota.admit(who, ids[1:], now=expected_end - timedelta(microseconds=1))
        across = await quota.admit(who, ids[:1], now=expected_end)

        assert first.period_end == expected_end, anchor
        assert inside.period_end == expected_end, "последняя микросекунда ещё в периоде"
        assert across.granted == tuple(ids[:1]), "граница — уже следующий период, карточка платная"
        assert await kit.period_count(user) == 2


# ── резерв, возврат, зависшие ───────────────────────────────────────────────


@scenario
async def a_failed_send_returns_the_slots_it_took_and_only_those(kit: Kit) -> None:
    user, who = await started(kit)
    seen, fresh = await kit.listings(1), await kit.listings(2)
    quota = service(kit)
    await quota.confirm(await quota.admit(who, seen))

    attempt = await quota.admit(who, [seen[0], *fresh])
    await quota.release(attempt)

    assert [row.listing_id for row in await kit.rows(user)] == seen, (
        "виденное чужое: его не откатить"
    )
    again = await quota.admit(who, fresh)
    assert again.granted == tuple(fresh) and again.remaining == FREE_CARDS_PER_PERIOD - 3


@scenario
async def a_confirmed_show_is_never_taken_back(kit: Kit) -> None:
    """Поздний `release` (гонка двух ответов) не стирает то, что уже дошло до человека."""
    user, who = await started(kit)
    ids = await kit.listings(3)
    quota = service(kit)
    admission = await quota.admit(who, ids)
    await quota.confirm(admission)

    await quota.release(admission)

    assert [row.listing_id for row in await kit.rows(user)] == ids
    assert all(row.delivered for row in await kit.rows(user))


@scenario
async def a_double_press_confirms_what_it_showed_while_the_first_send_is_in_flight(
    kit: Kit,
) -> None:
    """Двойное нажатие: вторая выдача видит резерв первой и показывает те же карточки.

    Показала — значит подтвердила: если первая отправка потом упадёт, возврат не должен
    стереть то, что вторая уже доставила, а счётчик запроса считает показанное, а не
    только списанное.
    """
    user, who = await started(kit)
    ids, request = await kit.listings(3), await kit.new_request(user)
    quota = service(kit)
    first = await quota.admit(who, ids)

    second = await quota.admit(who, ids, request_id=request)
    await quota.confirm(second)
    await quota.release(first)

    assert second.granted == () and second.repeated == tuple(ids)
    assert all(row.delivered for row in await kit.rows(user))
    assert [row.listing_id for row in await kit.rows(user)] == ids
    assert await kit.counters(request) == (3, 0)


@scenario
async def identify_finds_listings_by_the_pair_the_source_uses(kit: Kit) -> None:
    """Находка без `listing_id` опознаётся по паре «источник, внешний id»; чужой пары нет."""
    ids = await kit.listings(3)
    refs = [await kit.ref_of(listing_id) for listing_id in ids]
    quota = service(kit)

    found = await quota.identify([*refs, ("chotot", "no-such-card")])

    assert found == dict(zip(refs, ids, strict=True))
    assert await quota.identify([]) == {}


@scenario
async def a_hung_reservation_is_swept_but_a_confirmed_one_and_a_monitor_one_stay(kit: Kit) -> None:
    user, who = await started(kit)
    hung, kept, alerted = await kit.listings(3), await kit.listings(2), await kit.listings(2)
    quota = service(kit)
    await quota.admit(who, hung)
    await quota.confirm(await quota.admit(who, kept))
    await quota.admit(who, alerted, Channel.MONITOR)

    assert await quota.sweep(T0, 100) == 0, "ровно в момент резерва он ещё не «старше»"
    assert await quota.sweep(T0 + RESERVATION_TTL, 100) == 3

    left = {row.listing_id for row in await kit.rows(user)}
    assert left == set(kept) | set(alerted)
    assert (await quota.admit(who, hung)).granted == tuple(hung), "снятый резерв освободил слоты"


@scenario
async def the_sweep_works_in_batches(kit: Kit) -> None:
    user, who = await started(kit)
    ids = await kit.listings(5)
    quota = service(kit)
    await quota.admit(who, ids)

    assert await quota.sweep(T0 + RESERVATION_TTL, 2) == 2
    assert len(await kit.rows(user)) == 3


# ── кому и сколько ──────────────────────────────────────────────────────────


@scenario
async def the_owner_has_no_limit_but_the_journal_is_still_written(kit: Kit) -> None:
    user = await kit.new_user()
    boss = Account(user_id=user, tg_user_id=OWNER_TG)
    ids = await kit.listings(50)
    quota = service(kit, owner_tg_id=OWNER_TG)

    first = await quota.admit(boss, ids)
    replay = await quota.admit(boss, ids)

    assert first.granted == tuple(ids) and first.limit is None and first.remaining is None
    assert replay.repeated == tuple(ids) and replay.granted == ()
    assert len(await kit.rows(user)) == 50
    assert (await quota.standing(boss)).limit is None


@scenario
async def the_monitor_does_not_spend_the_ceiling_but_is_remembered(kit: Kit) -> None:
    user, who = await started(kit)
    pushed, searched = await kit.listings(20), await kit.listings(5)
    quota = service(kit)

    alert = await quota.admit(who, pushed, Channel.MONITOR)
    page = await quota.admit(who, [*searched, pushed[0]])

    assert alert.granted == tuple(pushed) and alert.limit is None
    assert page.granted == tuple(searched), "карточка слежения — не «новая» для поиска"
    assert page.repeated == (pushed[0],)
    assert page.remaining == FREE_CARDS_PER_PERIOD - 5, "слежение потолок не тратило"
    channels = {row.channel for row in await kit.rows(user)}
    assert channels == {"monitor", "search"}


@scenario
async def the_ceiling_is_decided_at_the_moment_of_issue_and_history_is_never_recounted(
    kit: Kit,
) -> None:
    user, who = await started(kit)
    slots = Slots(0)
    quota = service(kit, entitlements=slots)
    free = await quota.admit(who, await kit.listings(FREE_CARDS_PER_PERIOD))
    assert (free.limit, free.remaining) == (FREE_CARDS_PER_PERIOD, 0)

    slots.count = 1  # подписка началась посреди периода: окно то же, потолок выше
    paid = await quota.admit(who, await kit.listings(5))
    assert paid.limit == PAID_CARDS_PER_PERIOD
    assert paid.remaining == PAID_CARDS_PER_PERIOD - FREE_CARDS_PER_PERIOD - 5

    slots.count = 0  # подписка кончилась: выданное остаётся, нового бесплатно нет
    ended = await quota.admit(who, await kit.listings(3))
    assert ended.granted == () and len(ended.withheld) == 3 and ended.remaining == 0
    assert len(await kit.rows(user)) == FREE_CARDS_PER_PERIOD + 5


# ── счётчики запроса, предложение, положение ────────────────────────────────


@scenario
async def the_request_counts_what_was_shown_and_what_the_limit_held_back(kit: Kit) -> None:
    user, who = await started(kit)
    request = await kit.new_request(user)
    quota = service(kit)

    admission = await quota.admit(who, await kit.listings(12), request_id=request)

    assert len(admission.granted) == 10 and len(admission.withheld) == 2
    assert await kit.counters(request) == (0, 2), (
        "удержанное известно сразу, показанное — после отправки"
    )
    await quota.confirm(admission)
    assert await kit.counters(request) == (10, 2)


@scenario
async def a_subscription_is_offered_at_most_once_a_day(kit: Kit) -> None:
    _, who = await started(kit)
    clock = Clock(T0)
    quota = service(kit, clock)

    assert await quota.may_offer(who) is True
    assert await quota.may_offer(who) is False
    clock.tick(timedelta(hours=23, minutes=59))
    assert await quota.may_offer(who) is False
    clock.tick(timedelta(minutes=1))
    assert await quota.may_offer(who) is True


@scenario
async def the_standing_reads_without_writing(kit: Kit) -> None:
    user, who = await started(kit)
    quota = service(kit)

    assert await quota.standing(who) == Standing(used=0, limit=10, period_end=None)
    assert await kit.anchor(user) is None and await kit.period_count(user) == 0

    await quota.admit(who, await kit.listings(3))
    assert await quota.standing(who) == Standing(
        used=3, limit=10, period_end=datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
    )


@scenario
async def the_standing_ignores_the_monitor(kit: Kit) -> None:
    _, who = await started(kit)
    quota = service(kit)
    await quota.admit(who, await kit.listings(4), Channel.MONITOR)
    await quota.admit(who, await kit.listings(2))

    assert (await quota.standing(who)).used == 2


# ── один набор сценариев, две реализации ────────────────────────────────────


def scenario_id(function: Scenario) -> str:
    return function.__name__


def test_the_scenarios_are_not_lost() -> None:
    """Пустой набор превратил бы оба прогона ниже в зелёные впустую."""
    assert len(SCENARIOS) >= 20


@pytest.mark.parametrize("function", SCENARIOS, ids=scenario_id)
async def test_the_in_memory_ledger_keeps_the_contract(function: Scenario) -> None:
    await function(MemoryKit())


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL не задан: живого Postgres нет"
)
@pytest.mark.parametrize("function", SCENARIOS, ids=scenario_id)
async def test_the_sql_ledger_keeps_the_same_contract(
    function: Scenario, db_engine: AsyncEngine
) -> None:
    await function(SqlKit(db_engine))
