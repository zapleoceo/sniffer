"""Срок годности очереди: просроченное отменяется, а не доставляется вчерашним.

Простоя нотифаера хватает на сутки: деплой, упавший контейнер, длинный 429. После
него `outbox` держит «мгновенные» уведомления вчерашней давности, и раньше они
уходили как новые — клиент видел «новое объявление» о лоте, который уже продан.
Возраст — от времени, на которое строка назначена, а не от постановки: подборка на
вечер созревает вечером.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from sniffer.config import Settings, reload_settings
from sniffer.notifier import __main__ as entrypoint
from sniffer.notifier.policy import Policy, policy_from
from tests.bot_api_support import TOKEN
from tests.notifier_support import START, Boom, Clock, Row, Store, deliver

H = timedelta(hours=1)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instantly(_seconds: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", instantly)


def aged(identifier: int, age: timedelta, *, user_id: int = 7, **fields: object) -> Row:
    return Row(
        identifier,
        user_id=user_id,
        recipient_id=user_id + 35,
        scheduled_at=START - age,
        **fields,  # type: ignore[arg-type]
    )


async def test_a_row_older_than_a_day_is_cancelled_instead_of_arriving_late() -> None:
    store = Store([aged(1, 25 * H), aged(2, 23 * H, user_id=8)])
    delivery, telegram = deliver(store, Clock())

    assert await delivery.tick() == 1

    assert (store.row(1).status, store.row(1).last_error) == ("cancelled", "expired")
    assert store.row(2).status == "sent"
    assert telegram.recipients == [43], "вчерашнее не должно доходить как новое"


async def test_a_digest_is_old_from_its_scheduled_hour_not_from_the_morning_it_was_queued() -> None:
    """Подборка на 18:00 ставится утром: к вечеру ей час отроду, а не десять."""
    store = Store([aged(1, 1 * H, payload={"title": "x", "delivery_mode": "digest"})])
    delivery, telegram = deliver(store, Clock())

    assert await delivery.tick() == 1 and len(telegram.texts) == 1


async def test_the_right_to_follow_does_not_shorten_the_queue_term() -> None:
    """Шесть часов после срока подписки — правило матчера, а не нотифаера."""
    store = Store(
        [
            aged(1, 7 * H, subscription_id=10),  # срок вышел, строке семь часов
            aged(2, 7 * H, user_id=9, subscription_id=12),  # право есть
            aged(3, 7 * H, user_id=10, subscription_id=13),  # подписка на паузе
        ]
    )
    store.subscriptions = {10: (True, START - 2 * H), 12: (True, START + 5 * H), 13: (False, None)}
    delivery, _ = deliver(store, Clock())

    await delivery.tick()

    assert [store.row(i).status for i in (1, 2, 3)] == ["sent", "sent", "sent"]


async def test_a_deferred_answer_without_a_subscription_waits_the_ordinary_day() -> None:
    """У отложенного ответа после сбора подписки нет, и ждёт он общие сутки."""
    store = Store([aged(1, 7 * H)])
    delivery, telegram = deliver(store, Clock())

    assert await delivery.tick() == 1 and len(telegram.texts) == 1


async def test_the_term_comes_from_the_policy_not_from_a_constant() -> None:
    store = Store([aged(1, 3 * H)])
    delivery, telegram = deliver(store, Clock(), policy=Policy(ttl=2 * H))

    assert await delivery.tick() == 0

    assert store.row(1).status == "cancelled" and telegram.texts == []


async def test_the_age_is_measured_by_the_clock_of_the_pass() -> None:
    store, clock = Store([aged(1, 23 * H)]), Clock()
    delivery, telegram = deliver(store, clock)
    clock.advance(hours=2)

    assert await delivery.tick() == 0

    assert store.row(1).status == "cancelled" and telegram.texts == []


async def test_a_retried_row_is_judged_by_its_new_time_so_a_flaky_network_is_not_cancelled() -> (
    None
):
    """Повтор сдвигает `scheduled_at`; от застревания защищает потолок попыток, не срок."""
    store, clock = Store([aged(1, 23 * H)]), Clock()
    delivery, _ = deliver(store, clock, Boom("сеть"))

    await delivery.tick()

    assert store.row(1).status == "pending" and store.row(1).scheduled_at == START + timedelta(
        minutes=1
    )


# ── настройки ───────────────────────────────────────────────────────────────


@pytest.fixture
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Настройки без чужого `.env` и без переменных, которые тест задаёт сам."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OUTBOX_TTL_H", raising=False)


@pytest.mark.usefixtures("clean_environment")
def test_the_policy_defaults_are_the_settings_defaults() -> None:
    """Числа записаны в двух местах — в настройках и в политике; расхождение стерегут здесь."""
    assert policy_from(Settings()) == Policy()


@pytest.mark.usefixtures("clean_environment")
def test_the_term_is_tuned_by_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUTBOX_TTL_H", "12")

    policy = policy_from(Settings())

    assert policy.ttl == 12 * H


@pytest.mark.usefixtures("clean_environment")
def test_a_term_of_zero_hours_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нулевой срок отменял бы всё сразу: молча включить такое нельзя."""
    monkeypatch.setenv("OUTBOX_TTL_H", "0")

    with pytest.raises(ValueError, match="outbox_ttl_h"):
        Settings()


@pytest.mark.usefixtures("clean_environment")
async def test_the_process_builds_its_delivery_with_the_policy_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Настройка, которую процесс не читает, — это константа с красивым названием."""
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("OUTBOX_TTL_H", "12")
    reload_settings()
    seen: list[Policy | None] = []

    class SpyDelivery:
        def __init__(
            self, send: object, *, send_in_thread: object = None, policy: Policy | None = None
        ) -> None:
            seen.append(policy)

        async def tick(self, *, now: object = None) -> int:
            return 0

    async def no_loop(
        stop: asyncio.Event, tick: object, *, service: str, poll_interval_s: float
    ) -> None:
        return None

    monkeypatch.setattr(entrypoint, "Delivery", SpyDelivery)
    monkeypatch.setattr(entrypoint, "idle_loop", no_loop)

    try:
        await entrypoint.run(asyncio.Event())
    finally:
        monkeypatch.delenv("OUTBOX_TTL_H")
        reload_settings()

    assert seen and seen[0] is not None and seen[0].ttl == 12 * H
