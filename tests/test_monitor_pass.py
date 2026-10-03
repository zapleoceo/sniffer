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

from sniffer.worker import __main__ as worker_main
from sniffer.worker.matcher import Matcher
from tests.monitor_support import NOW, install, listing, subscription, usd_subscription

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

    assert [claim["now"] for claim in world.delivery.claims] == [LATER]
    assert [item["now"] for item in world.delivery.queued] == [LATER]


async def test_without_an_argument_the_pass_asks_its_injected_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Часы — внедряемая зависимость, а не вызов `datetime.now` посреди прохода."""
    world = install(monkeypatch, subscriptions=[subscription()], page=[listing(moment=LATER)])

    assert await Matcher(clock=lambda: LATER).tick() == 1

    assert [claim["now"] for claim in world.delivery.claims] == [LATER]
    assert [item["now"] for item in world.delivery.queued] == [LATER]


async def test_an_explicit_moment_beats_the_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    world = install(monkeypatch, subscriptions=[subscription()], page=[listing()])

    await Matcher(clock=lambda: LATER).tick(now=NOW)

    assert [claim["now"] for claim in world.delivery.claims] == [NOW]


async def test_the_default_clock_is_the_real_utc_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без внедрения проход живёт по настоящему времени, и оно с поясом, а не наивное."""
    world = install(monkeypatch, subscriptions=[subscription()])
    before = datetime.now(UTC)

    await Matcher().tick()

    (claim,) = world.delivery.claims
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
