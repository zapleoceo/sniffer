"""Проход монитора без базы: чьими часами он живёт, что и кому передаёт.

Репозитории подменены (`monitor_support`): здесь проверяется порядок вызовов и аргументы,
а не SQL. Тот же проход на живом Postgres — `test_db_monitor.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sniffer.worker.matcher import Matcher
from tests.monitor_support import NOW, install, listing, subscription

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
