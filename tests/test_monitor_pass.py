"""Проход монитора без базы: чьими часами он живёт, что и кому передаёт.

Репозитории подменены (`monitor_support`): здесь проверяется порядок вызовов и аргументы,
а не SQL. Тот же проход на живом Postgres — `test_db_monitor.py`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from structlog.testing import capture_logs

from sniffer.db.repositories.monitors import BrokenSubscription
from sniffer.worker import __main__ as worker_main
from sniffer.worker.matcher import Matcher
from sniffer.worker.quarantine import QUARANTINE_FIRST, QUARANTINE_MAX, quarantine_delay
from tests.monitor_support import (
    NOW,
    install,
    listing,
    subscription,
    subscription_in,
    usd_subscription,
)

LATER = NOW + timedelta(days=3)


# ── часы (D6): проход живёт одним моментом, который ему дали ────────────────


async def test_the_pass_hands_one_moment_to_every_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """`now` из аргумента обязан дойти и до выбора подписок, и до постановки в очередь.

    Живой дефект D6: `tick(now=...)` доходил только до оценки карточек, а подписки выбирались
    по часам машины. Тест, который передаёт время «из будущего», этого не видел: выбор и
    оценка смотрели на разные «сейчас».
    """
    world = install(monkeypatch, subscriptions=[subscription()], page=[listing(moment=LATER)])

    assert await Matcher().tick(now=LATER) == 1

    assert [claim["now"] for claim in world.monitors.claims] == [LATER]
    assert [item["now"] for item in world.delivery.queued] == [LATER]


async def test_without_an_argument_the_pass_asks_its_injected_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Часы — внедряемая зависимость, а не вызов `datetime.now` посреди прохода."""
    world = install(monkeypatch, subscriptions=[subscription()], page=[listing(moment=LATER)])

    assert await Matcher(clock=lambda: LATER).tick() == 1

    assert [claim["now"] for claim in world.monitors.claims] == [LATER]
    assert [item["now"] for item in world.delivery.queued] == [LATER]


async def test_an_explicit_moment_beats_the_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    world = install(monkeypatch, subscriptions=[subscription()], page=[listing()])

    await Matcher(clock=lambda: LATER).tick(now=NOW)

    assert [claim["now"] for claim in world.monitors.claims] == [NOW]


async def test_the_default_clock_is_the_real_utc_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без внедрения проход живёт по настоящему времени, и оно с поясом, а не наивное."""
    world = install(monkeypatch, subscriptions=[subscription()])
    before = datetime.now(UTC)

    await Matcher().tick()

    (claim,) = world.monitors.claims
    assert claim["now"].tzinfo is not None
    assert before <= claim["now"] <= datetime.now(UTC)


# ── курс доллара (D2) ───────────────────────────────────────────────────────

RATE = 26_000.0


class Rates:
    """Источник курса с заранее заданными ответами; последний повторяется."""

    def __init__(self, *answers: float | BaseException | None) -> None:
        self._answers = list(answers)
        self.calls = 0

    async def __call__(self) -> float | None:
        self.calls += 1
        answer = self._answers[min(self.calls, len(self._answers)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer


async def test_a_dollar_budget_reaches_the_query_as_a_dong_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """300 $ при 26 000 ₫/$ — потолок 7,8 млн в самом запросе, а не «любая цена»."""
    world = install(monkeypatch, subscriptions=[usd_subscription()], page=[listing()])

    await Matcher(rate=Rates(RATE)).tick(now=NOW)

    ((spec, _after, _limit),) = world.listings.asked
    assert spec.max_price_vnd == Decimal("7800000")


async def test_without_a_rate_a_dollar_slot_waits_instead_of_ignoring_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Нет курса — подписка ждёт: ничего не шлёт и курсор не двигает.

    Прежний матчер строил отбор без потолка и отдавал дорогое как «идеально в бюджете».
    """
    world = install(monkeypatch, subscriptions=[usd_subscription()], page=[listing()])
    matcher = Matcher(rate=Rates(None))

    with capture_logs() as logs:
        assert await matcher.tick(now=NOW) == 0

    assert world.listings.asked == [], "запрос без потолка не уходит"
    assert world.delivery.queued == [] and world.delivery.advanced == []
    assert matcher.counters.skipped_no_rate == 1
    waiting = [entry for entry in logs if entry["event"] == "matcher.waiting_for_rate"]
    assert waiting and waiting[0]["subscriptions"] == [1]


async def test_a_matcher_without_a_rate_source_waits_too(monkeypatch: pytest.MonkeyPatch) -> None:
    world = install(monkeypatch, subscriptions=[usd_subscription()], page=[listing()])

    with capture_logs() as logs:
        assert await Matcher().tick(now=NOW) == 0

    assert world.listings.asked == []
    failed = [entry for entry in logs if entry["event"] == "fx.usd_rate_failed"]
    assert not failed, "источник не задан — это настройка, а не отказ сервиса"


async def test_a_dong_slot_is_served_while_a_dollar_slot_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = install(
        monkeypatch, subscriptions=[usd_subscription(1), subscription(2)], page=[listing()]
    )

    assert await Matcher(rate=Rates(None)).tick(now=NOW) == 1

    assert [item["subscription_id"] for item in world.delivery.queued] == [2]


async def test_the_rate_is_not_asked_when_no_slot_needs_it(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, subscriptions=[subscription(1), subscription(2)], page=[listing()])
    rates = Rates(RATE)

    await Matcher(rate=rates).tick(now=NOW)

    assert rates.calls == 0


async def test_a_dollar_slot_resumes_from_the_same_place_when_the_rate_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = install(monkeypatch, subscriptions=[usd_subscription()], page=[listing()])
    matcher = Matcher(rate=Rates(None, RATE))

    assert await matcher.tick(now=NOW) == 0
    assert await matcher.tick(now=NOW + timedelta(minutes=2)) == 1

    ((_spec, after_id, _limit),) = world.listings.asked
    assert after_id == 0, "курсор стоял: ничего не пропущено"


async def test_a_failed_rate_is_not_asked_again_on_every_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Источник курса, который не ответил, не опрашивают каждые пять секунд."""
    install(monkeypatch, subscriptions=[usd_subscription()], page=[listing()])
    rates = Rates(None)
    matcher = Matcher(rate=rates)

    await matcher.tick(now=NOW)
    await matcher.tick(now=NOW + timedelta(seconds=5))
    assert rates.calls == 1, "в пределах паузы источник не трогаем"

    await matcher.tick(now=NOW + timedelta(minutes=2))
    assert rates.calls == 2, "после паузы спрашиваем снова"


async def test_a_dead_rate_source_is_logged_once_per_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, subscriptions=[usd_subscription()], page=[listing()])
    matcher = Matcher(rate=Rates(None))

    with capture_logs() as logs:
        for minutes in (0, 2, 4):
            await matcher.tick(now=NOW + timedelta(minutes=minutes))

    down = [entry for entry in logs if entry["event"] == "fx.usd_rate_unavailable"]
    assert len(down) == 1, "простой — одна запись, а не по записи на попытку"


async def test_an_exploding_rate_source_is_just_an_unavailable_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Источник — чужой сервис: его отказ не должен ронять проход и соседние подписки."""
    world = install(
        monkeypatch, subscriptions=[usd_subscription(1), subscription(2)], page=[listing()]
    )

    assert await Matcher(rate=Rates(RuntimeError("сервис курса упал"))).tick(now=NOW) == 1

    assert [item["subscription_id"] for item in world.delivery.queued] == [2]


@pytest.mark.parametrize("nonsense", [0.0, -26_000.0, float("nan"), float("inf")])
async def test_a_nonsense_rate_is_no_rate(monkeypatch: pytest.MonkeyPatch, nonsense: float) -> None:
    """NaN в Postgres больше любого числа: потолок «до NaN» пропустил бы всё."""
    world = install(monkeypatch, subscriptions=[usd_subscription()], page=[listing()])

    assert await Matcher(rate=Rates(nonsense)).tick(now=NOW) == 0

    assert world.listings.asked == []


@pytest.mark.parametrize("stop", [KeyboardInterrupt(), asyncio.CancelledError()])
async def test_a_stop_signal_in_the_rate_source_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, stop: BaseException
) -> None:
    """Остановка процесса — не отказ источника: `except Exception` её не ловит."""
    install(monkeypatch, subscriptions=[usd_subscription()], page=[listing()])

    with pytest.raises(type(stop)):
        await Matcher(rate=Rates(stop)).tick(now=NOW)


async def test_the_worker_gives_the_matcher_the_live_dollar_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проводка в точке входа: дефект D2 сидел именно в ней, а не в самом матчере."""
    answers = Rates(RATE)
    monkeypatch.setattr(worker_main, "usd_vnd_rate", answers)
    world = install(monkeypatch, subscriptions=[usd_subscription()], page=[listing()])

    await worker_main.build_matcher().tick(now=NOW)

    assert answers.calls == 1
    ((spec, _after, _limit),) = world.listings.asked
    assert spec.max_price_vnd == Decimal("7800000")


# ── обход по кругу, изоляция подписок и карантин (D4, D5) ───────────────────

BOOM = "boom"


async def test_a_failing_slot_is_quarantined_and_the_others_are_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Одна больная подписка не роняет проход: остальные получают свои карточки.

    Прежний проход шёл одной транзакцией без охраны на подписку, и любое исключение
    уносило воркер: Docker перезапускал процесс, цикл повторялся, стояла вся воронка (D5).
    """
    world = install(
        monkeypatch,
        subscriptions=[
            subscription(1),
            subscription_in(2, BOOM),
            subscription(3),
        ],
        page=[listing()],
        explode={BOOM: ValueError("незнакомое значение")},
    )
    matcher = Matcher()

    assert await matcher.tick(now=NOW) == 2

    assert [item["subscription_id"] for item in world.delivery.queued] == [1, 3]
    (isolated,) = world.monitors.quarantined
    assert isolated["id"] == 2 and isolated["streak"] == 1
    assert isolated["until"] == NOW + quarantine_delay(1)
    assert "ValueError" in isolated["error"] and "незнакомое значение" in isolated["error"]
    assert world.monitors.scanned == [1, 3], "обойдёнными отмечены только удачные"
    assert matcher.counters.quarantined == 1


async def test_every_slot_works_in_its_own_savepoint_and_only_the_failing_one_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = install(
        monkeypatch,
        subscriptions=[subscription(1), subscription_in(2, BOOM), subscription(3)],
        page=[listing()],
        explode={BOOM: RuntimeError("упала")},
    )

    await Matcher().tick(now=NOW)

    assert world.session.savepoints == 3, "по SAVEPOINT на подписку"
    assert world.session.rolled_back == 1, "откат — только у больной"
    assert world.session.commits == 1, "коммит прохода один, в конце"


async def test_the_streak_grows_and_the_pause_backs_off(monkeypatch: pytest.MonkeyPatch) -> None:
    world = install(
        monkeypatch,
        subscriptions=[subscription_in(1, BOOM, failed_streak=2)],
        explode={BOOM: RuntimeError("снова")},
    )

    await Matcher().tick(now=NOW)

    (isolated,) = world.monitors.quarantined
    assert isolated["streak"] == 3
    assert isolated["until"] == NOW + quarantine_delay(3) == NOW + timedelta(minutes=20)


async def test_a_slot_that_cannot_be_read_goes_to_quarantine_with_the_reason_it_came_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Паспорт с незнакомым значением падает ещё при разборе строки, до всякой подписки."""
    sick = BrokenSubscription(
        id=7, user_id=70, failed_streak=1, error="ValueError: 'spaceship' is not a valid Category"
    )
    world = install(monkeypatch, subscriptions=[subscription(1)], broken=[sick], page=[listing()])
    matcher = Matcher()

    assert await matcher.tick(now=NOW) == 1

    (isolated,) = world.monitors.quarantined
    assert isolated["id"] == 7 and isolated["streak"] == 2
    assert isolated["error"] == sick.error
    assert isolated["until"] == NOW + quarantine_delay(2)
    assert matcher.counters.quarantined == 1


@pytest.mark.parametrize("stop", [KeyboardInterrupt(), asyncio.CancelledError()])
async def test_a_stop_signal_in_a_slot_is_not_a_slot_failure(
    monkeypatch: pytest.MonkeyPatch, stop: BaseException
) -> None:
    """SIGTERM — просьба остановиться, а не поломка подписки: в карантин за неё не отправляют."""
    world = install(
        monkeypatch,
        subscriptions=[subscription_in(1, BOOM)],
        explode={BOOM: stop},
    )

    with pytest.raises(type(stop)):
        await Matcher().tick(now=NOW)

    assert world.monitors.quarantined == []
    assert world.session.commits == 0


async def test_a_failure_of_the_claim_itself_is_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """Упала база, а не подписка: изолировать нечего, и молчать об этом нельзя."""
    world = install(monkeypatch)
    world.monitors.claim_error = ConnectionError("база недоступна")

    with pytest.raises(ConnectionError):
        await Matcher().tick(now=NOW)


async def test_the_quarantine_is_logged_as_an_error_with_its_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cause = RuntimeError("упала")
    install(monkeypatch, subscriptions=[subscription_in(4, BOOM)], explode={BOOM: cause})

    with capture_logs() as logs:
        await Matcher().tick(now=NOW)

    (entry,) = [item for item in logs if item["event"] == "matcher.quarantined"]
    assert entry["log_level"] == "error"
    assert entry["subscription"] == 4 and entry["user"] == 104 and entry["streak"] == 1
    assert entry["exc_info"] is cause, "в журнале видна причина, а не только её текст"


async def test_every_outcome_marks_the_slot_as_visited_for_the_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Обойдёнными считаются и подписки, которым нечего отдать: иначе они стояли бы первыми
    и занимали порцию вечно (D4)."""
    world = install(
        monkeypatch,
        subscriptions=[subscription(1), subscription_in(2, None)],
        page=[listing()],
    )

    await Matcher().tick(now=NOW)

    assert world.monitors.scanned == [1, 2]


async def test_a_slot_that_reached_its_daily_cap_is_still_visited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = install(monkeypatch, subscriptions=[subscription(1)], page=[listing()])
    world.delivery.used = 5

    assert await Matcher().tick(now=NOW) == 0

    assert world.monitors.scanned == [1]


async def test_a_waiting_slot_is_visited_but_not_scanned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ждущая курса подписка уходит в конец очереди обхода, а курсор и сбои не трогает."""
    world = install(monkeypatch, subscriptions=[usd_subscription(1)], page=[listing()])

    await Matcher(rate=Rates(None)).tick(now=NOW)

    assert world.monitors.touched == [1]
    assert world.monitors.scanned == [], "`record_scan` снял бы следы сбоев, а подписку не смотрели"


async def test_the_pass_takes_the_batch_it_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    world = install(monkeypatch)

    await Matcher(batch=7).tick(now=NOW)

    assert [claim["limit"] for claim in world.monitors.claims] == [7]


async def test_the_default_batch_comes_from_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Размер порции — константа в конфиге (D4), а не число, зашитое в проход."""
    from sniffer.config import reload_settings

    world = install(monkeypatch)
    try:
        await Matcher().tick(now=NOW)
        monkeypatch.setenv("MONITOR_BATCH", "9")
        reload_settings()
        await Matcher().tick(now=NOW)
    finally:
        monkeypatch.delenv("MONITOR_BATCH")
        reload_settings()

    assert [claim["limit"] for claim in world.monitors.claims] == [50, 9]


# ── пауза карантина: чистая функция ─────────────────────────────────────────


def test_the_pause_doubles_with_every_failure_and_hits_a_ceiling() -> None:
    minutes = [quarantine_delay(streak) / timedelta(minutes=1) for streak in range(1, 10)]

    assert minutes == [5, 10, 20, 40, 80, 160, 320, 360, 360]
    assert quarantine_delay(1) == QUARANTINE_FIRST
    assert quarantine_delay(99) == QUARANTINE_MAX


@pytest.mark.parametrize("streak", [0, -3])
def test_a_nonsense_streak_gets_the_first_pause(streak: int) -> None:
    assert quarantine_delay(streak) == QUARANTINE_FIRST


def test_a_huge_streak_does_not_overflow() -> None:
    """`timedelta * 2**1000` — OverflowError: счёт сбоев не вправе ронять карантин."""
    assert quarantine_delay(10**6) == QUARANTINE_MAX


# ── право и льгота (D7) ─────────────────────────────────────────────────────


async def test_the_pass_cancels_the_lapsed_queue_before_it_takes_new_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проход, который ставит новое, не оставляет за собой просроченное старое."""
    world = install(monkeypatch, subscriptions=[subscription()], page=[listing()])
    world.monitors.lapsed = 3
    matcher = Matcher()

    await matcher.tick(now=NOW)

    assert world.monitors.order[:2] == ["cancel", "claim"]
    assert world.monitors.cancellations == [{"now": NOW, "grace": timedelta(hours=6)}]
    assert matcher.counters.cancelled_lapsed == 3


async def test_the_cancellation_is_logged_only_when_something_was_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = install(monkeypatch)
    matcher = Matcher()

    with capture_logs() as quiet:
        await matcher.tick(now=NOW)
    world.monitors.lapsed = 2
    with capture_logs() as loud:
        await matcher.tick(now=NOW)

    assert not [entry for entry in quiet if entry["event"] == "matcher.lapsed_cancelled"]
    (entry,) = [entry for entry in loud if entry["event"] == "matcher.lapsed_cancelled"]
    assert entry["cancelled"] == 2
    assert matcher.counters.cancelled_lapsed == 2


async def test_the_grace_period_is_an_explicit_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    world = install(monkeypatch)

    await Matcher(lapse_grace=timedelta(minutes=30)).tick(now=NOW)

    assert world.monitors.cancellations[0]["grace"] == timedelta(minutes=30)


async def test_the_default_grace_period_comes_from_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Льгота — константа в конфиге (D7), а не число, зашитое в проход."""
    from sniffer.config import reload_settings

    world = install(monkeypatch)
    try:
        monkeypatch.setenv("MONITOR_LAPSE_GRACE_HOURS", "2")
        reload_settings()
        await Matcher().tick(now=NOW)
    finally:
        monkeypatch.delenv("MONITOR_LAPSE_GRACE_HOURS")
        reload_settings()

    assert world.monitors.cancellations[0]["grace"] == timedelta(hours=2)
