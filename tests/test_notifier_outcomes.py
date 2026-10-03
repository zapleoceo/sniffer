"""Исход отправки: что значит каждый ответ Bot API и что с ним делает очередь.

Исключения здесь настоящие: их строит `BaseSession.check_response` самой aiogram
из HTTP-статуса и тела (`tests/bot_api_support.py`), а не тест. Решает не класс
исключения, а `classify`; всё, чего он не знает, — временный сбой с потолком попыток.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage

from sniffer.notifier.outcome import DETAIL_LIMIT, Failure, Kind, classify
from sniffer.notifier.policy import Action, Policy, backoff, decide
from tests.bot_api_support import TOKEN, Reply, refusal, telegram_error
from tests.notifier_support import START, Boom, Clock, Row, Store, deliver, digest_row

ANSWERS: dict[str, tuple[Reply, Kind, str]] = {
    "blocked": (refusal(403, "Forbidden: bot was blocked by the user"), Kind.BLOCKED, "forbidden"),
    "deactivated": (refusal(403, "Forbidden: user is deactivated"), Kind.BLOCKED, "forbidden"),
    "never_started": (
        refusal(403, "Forbidden: bot can't initiate conversation with a user"),
        Kind.BLOCKED,
        "forbidden",
    ),
    "chat_not_found": (
        refusal(400, "Bad Request: chat not found"),
        Kind.BLOCKED,
        "chat_unavailable",
    ),
    "flood": (
        refusal(429, "Too Many Requests: retry after 17", retry_after=17),
        Kind.RATE_LIMITED,
        "rate_limited",
    ),
    "too_long": (refusal(400, "Bad Request: message is too long"), Kind.REJECTED, "too_long"),
    "thread": (
        refusal(400, "Bad Request: message thread not found"),
        Kind.REJECTED,
        "thread_not_found",
    ),
    "markup": (
        refusal(400, "Bad Request: can't parse entities: Can't find end of the entity"),
        Kind.REJECTED,
        "bad_markup",
    ),
    "unseen_400": (refusal(400, "Bad Request: nobody has seen this"), Kind.REJECTED, "bad_request"),
    "migrated": (
        refusal(400, "Bad Request: group chat was upgraded", migrate_to_chat_id=-1001234567),
        Kind.REJECTED,
        "migrated",
    ),
    "token": (refusal(401, "Unauthorized"), Kind.SYSTEM, "token_rejected"),
    "not_found": (refusal(404, "Not Found"), Kind.SYSTEM, "token_rejected"),
    "server": (refusal(500, "Internal Server Error"), Kind.TRANSIENT, "transient"),
    "gateway": (refusal(502, "Bad Gateway"), Kind.TRANSIENT, "transient"),
    "restart": (refusal(500, "Internal Server Error: restart"), Kind.TRANSIENT, "transient"),
}


@pytest.mark.parametrize("name", sorted(ANSWERS))
def test_a_telegram_answer_becomes_the_documented_outcome(name: str) -> None:
    reply, kind, reason = ANSWERS[name]

    failure = classify(telegram_error(reply))

    assert (failure.kind, failure.reason) == (kind, reason)


def test_a_flood_wait_carries_exactly_the_seconds_telegram_asked_for() -> None:
    failure = classify(telegram_error(ANSWERS["flood"][0]))

    assert failure.retry_after == 17


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("сеть отвалилась"),
        TimeoutError(),
        Boom("чужой тип"),
        TelegramNetworkError(method=SendMessage(chat_id=1, text="x"), message="ClientOSError"),
        ExceptionGroup("группа", [Boom(), ValueError()]),
    ],
    ids=["connection", "timeout", "boom", "aiogram_network", "plain_group"],
)
def test_everything_unknown_is_a_transient_failure_not_a_crash(error: Exception) -> None:
    assert classify(error).kind is Kind.TRANSIENT


@pytest.mark.parametrize(
    "interrupt",
    [
        KeyboardInterrupt(),
        asyncio.CancelledError(),
        SystemExit(3),
        GeneratorExit(),
        BaseExceptionGroup("все прерывания", [KeyboardInterrupt()]),
        BaseExceptionGroup("смешанная", [Boom(), asyncio.CancelledError()]),
    ],
    ids=["ctrl_c", "cancelled", "exit", "generator", "group_of_interrupts", "mixed_group"],
)
def test_a_request_to_stop_is_never_turned_into_an_outcome(interrupt: BaseException) -> None:
    """Проглоченный Ctrl+C — процесс, который не останавливается; проглоченный exit — чужой код."""
    with pytest.raises(type(interrupt)):
        classify(interrupt)


def test_the_bot_token_never_reaches_the_log_or_the_database() -> None:
    leaky = Boom(f"POST https://api.telegram.org/bot{TOKEN}/sendMessage " + "x" * 500)

    failure = classify(leaky)

    assert TOKEN not in failure.detail and "<token>" in failure.detail
    assert len(failure.detail) <= DETAIL_LIMIT


# ── решение по исходу ───────────────────────────────────────────────────────

POLICY = Policy()


def failure_of(name: str) -> Failure:
    return classify(telegram_error(ANSWERS[name][0]))


def test_backoff_doubles_and_never_exceeds_the_cap() -> None:
    delays = [backoff(POLICY, attempts) for attempts in range(10)]

    assert delays[:5] == [timedelta(minutes=minutes) for minutes in (1, 2, 4, 8, 16)]
    assert max(delays) == POLICY.backoff_cap and delays == sorted(delays)
    assert backoff(POLICY, 10_000) == POLICY.backoff_cap, "огромный счётчик не должен переполнять"


def test_a_transient_failure_retries_with_backoff_until_the_last_attempt() -> None:
    boom = classify(Boom("сеть"))

    first = decide(boom, attempts=0, now=START, policy=POLICY)
    last = decide(boom, attempts=POLICY.max_attempts - 1, now=START, policy=POLICY)

    assert (first.action, first.until) == (Action.RETRY, START + timedelta(minutes=1))
    assert last.action is Action.GIVE_UP and last.note.startswith("attempts_exhausted")


def test_a_flood_wait_pauses_for_what_telegram_asked_and_charges_no_attempt() -> None:
    verdict = decide(failure_of("flood"), attempts=0, now=START, policy=POLICY)

    assert (verdict.action, verdict.until) == (Action.PAUSE, START + timedelta(seconds=17))


def test_a_broken_token_pauses_instead_of_burning_the_attempts_of_every_message() -> None:
    verdict = decide(failure_of("token"), attempts=0, now=START, policy=POLICY)

    assert (verdict.action, verdict.until) == (Action.PAUSE, START + POLICY.system_pause)


@pytest.mark.parametrize("name", ["too_long", "thread", "markup", "unseen_400"])
def test_a_rejected_message_is_given_up_at_once(name: str) -> None:
    assert decide(failure_of(name), attempts=0, now=START, policy=POLICY).action is Action.GIVE_UP


@pytest.mark.parametrize("name", ["blocked", "deactivated", "never_started", "chat_not_found"])
def test_an_unreachable_client_blocks_the_whole_queue_not_one_message(name: str) -> None:
    assert decide(failure_of(name), attempts=0, now=START, policy=POLICY).action is Action.BLOCK


@pytest.mark.parametrize("kind", list(Kind))
def test_every_outcome_has_a_decision(kind: Kind) -> None:
    """Добавили исход и забыли решение — `match` вернул бы `None` и проход упал бы позже."""
    verdict = decide(Failure(kind, "x", retry_after=1), attempts=0, now=START, policy=POLICY)

    assert isinstance(verdict.action, Action)


# ── что очередь делает с исходом ────────────────────────────────────────────


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instantly(_seconds: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", instantly)


async def test_forbidden_marks_the_client_blocked_and_cancels_their_whole_queue() -> None:
    """Остальные строки того же клиента ушли бы с тем же отказом — лишние запросы к Bot API."""
    store = Store([Row(1), digest_row(2), digest_row(3), Row(4, user_id=8, recipient_id=43)])
    clock = Clock()
    delivery, telegram = deliver(store, clock, telegram_error(ANSWERS["blocked"][0]))

    assert await delivery.tick() == 1

    statuses = [store.row(i).status for i in (1, 2, 3, 4)]
    assert statuses == ["cancelled", "cancelled", "cancelled", "sent"]
    assert store.blocked == {42: START}, "метка — момент отказа, по часам нотифаера"
    assert (store.row(1).last_error or "").startswith("forbidden")
    assert telegram.recipients == [42, 43], "клиента с отказом не трогали второй раз"


async def test_a_row_queued_for_a_blocked_client_is_cancelled_without_a_send() -> None:
    """Очередь наполняют матчер и сборщик ответов: ни один не обязан помнить про блокировку."""
    store = Store([Row(1)])
    store.blocked = {42: START}
    delivery, telegram = deliver(store, Clock())

    assert await delivery.tick() == 0

    assert telegram.texts == []
    assert (store.row(1).status, store.row(1).last_error) == ("cancelled", "bot_blocked")


async def test_a_flood_wait_stops_the_whole_notifier_for_exactly_that_long() -> None:
    store = Store([Row(1), Row(2, user_id=8, recipient_id=43)])
    clock = Clock()
    delivery, telegram = deliver(store, clock, telegram_error(ANSWERS["flood"][0]))

    assert await delivery.tick() == 0

    assert telegram.recipients == [42], "просили подождать всех: второго клиента даже не пробуем"
    row = store.row(1)
    assert (row.status, row.attempts, row.scheduled_at) == ("pending", 0, START), (
        "429 — не вина сообщения: ни попытки, ни «+15 минут»"
    )
    clock.advance(seconds=16)
    assert await delivery.tick() == 0 and telegram.recipients == [42], "срок ещё не вышел"
    clock.advance(seconds=2)
    assert await delivery.tick() == 2, "срок вышел: оба уходят"


async def test_a_message_telegram_rejects_is_given_up_at_once_with_the_reason() -> None:
    store, clock = Store([Row(1)]), Clock()
    delivery, telegram = deliver(store, clock, telegram_error(ANSWERS["too_long"][0]))

    await delivery.tick()

    row = store.row(1)
    assert (row.status, row.attempts) == ("failed", 1)
    assert (row.last_error or "").startswith("too_long")
    clock.advance(hours=3)
    await delivery.tick()
    assert len(telegram.texts) == 1, "повтор дал бы тот же отказ: не повторяем"


async def test_a_vanished_thread_is_a_classified_rejection_not_an_endless_retry() -> None:
    store, clock = Store([Row(1)]), Clock()
    delivery, _ = deliver(store, clock, telegram_error(ANSWERS["thread"][0]))

    await delivery.tick()

    assert store.row(1).status == "failed"
    assert (store.row(1).last_error or "").startswith("thread_not_found")


async def test_unknown_failures_back_off_exponentially_and_then_stop() -> None:
    store, clock = Store([Row(1)]), Clock()
    delivery, telegram = deliver(store, clock, *[Boom("сеть")] * POLICY.max_attempts)
    waited: list[timedelta] = []

    for _ in range(POLICY.max_attempts):
        started = clock.now
        await delivery.tick()
        if store.row(1).status == "pending":
            waited.append(store.row(1).scheduled_at - started)
        clock.now = max(clock.now, store.row(1).scheduled_at)

    assert waited == [timedelta(minutes=minutes) for minutes in (1, 2, 4, 8, 16)]
    row = store.row(1)
    assert (row.status, row.attempts) == ("failed", POLICY.max_attempts)
    assert (row.last_error or "").startswith("attempts_exhausted")
    assert len(telegram.texts) == POLICY.max_attempts


async def test_a_broken_token_pauses_the_notifier_and_costs_the_message_nothing() -> None:
    store, clock = Store([Row(1)]), Clock()
    delivery, telegram = deliver(store, clock, telegram_error(ANSWERS["token"][0]))

    assert await delivery.tick() == 0

    assert (store.row(1).status, store.row(1).attempts) == ("pending", 0)
    clock.advance(minutes=4)
    assert await delivery.tick() == 0 and len(telegram.texts) == 1, "пауза не кончилась"
    clock.advance(minutes=2)
    assert await delivery.tick() == 1, "токен починили: сообщение не потеряно"


async def test_the_reason_a_row_stayed_behind_never_contains_the_bot_token() -> None:
    store, clock = Store([Row(1)]), Clock()
    delivery, _ = deliver(store, clock, Boom(f"https://api.telegram.org/bot{TOKEN}/sendMessage"))

    await delivery.tick()

    assert TOKEN not in (store.row(1).last_error or "")
    assert "<token>" in (store.row(1).last_error or "")


async def test_a_successful_send_clears_the_reason_of_an_earlier_failure() -> None:
    store, clock = Store([Row(1)]), Clock()
    delivery, _ = deliver(store, clock, Boom("сеть"))

    await delivery.tick()
    assert store.row(1).last_error
    clock.advance(minutes=2)
    await delivery.tick()

    assert (store.row(1).status, store.row(1).last_error) == ("sent", None)
