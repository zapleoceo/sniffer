"""Отправка через настоящий `Bot` aiogram: поля запроса и ответы Bot API от начала до конца.

Сессия подставная (`tests/bot_api_support.py`), всё остальное настоящее: `Bot`,
`SendMessage`, разбор статуса в исключение и наш `classify`. Лишнее или неверное
поле запроса поймал бы pydantic самой aiogram, а не вручную написанная заглушка.
"""

from __future__ import annotations

import pytest
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import LinkPreviewOptions

from sniffer.notifier import __main__ as entrypoint
from sniffer.notifier.delivery import Delivery
from sniffer.notifier.outcome import Kind, classify
from tests.bot_api_support import SENT, refusal, scripted_bot
from tests.notifier_support import Clock, Row, Store


async def test_the_sender_asks_for_html_and_no_link_preview_and_nothing_else() -> None:
    bot, session = scripted_bot(SENT)

    await entrypoint._sender(bot)(42, "<b>Honda</b>")

    (call,) = session.calls
    assert isinstance(call, SendMessage)
    assert (call.chat_id, call.text, call.parse_mode) == (42, "<b>Honda</b>", ParseMode.HTML)
    assert isinstance(call.link_preview_options, LinkPreviewOptions)
    assert call.link_preview_options.is_disabled
    assert not call.model_extra, "поле вне схемы Bot API — запрос, который Telegram не поймёт"


async def test_a_real_403_travels_through_the_sender_and_means_blocked() -> None:
    bot, _ = scripted_bot(refusal(403, "Forbidden: bot was blocked by the user"))

    with pytest.raises(TelegramForbiddenError) as raised:
        await entrypoint._sender(bot)(42, "x")

    assert classify(raised.value).kind is Kind.BLOCKED


async def test_a_real_429_carries_retry_after_through_the_sender() -> None:
    bot, _ = scripted_bot(refusal(429, "Too Many Requests: retry after 9", retry_after=9))

    with pytest.raises(TelegramRetryAfter) as raised:
        await entrypoint._sender(bot)(42, "x")

    failure = classify(raised.value)
    assert (failure.kind, failure.retry_after) == (Kind.RATE_LIMITED, 9)


async def test_the_whole_chain_from_a_403_to_a_blocked_client() -> None:
    """Настоящий `Bot` и сессия по сценарию → нотифаер → очередь: клиент помечен, очередь снята."""
    bot, session = scripted_bot(refusal(403, "Forbidden: bot was blocked by the user"))
    store, clock = Store([Row(1), Row(2)]), Clock()
    delivery = Delivery(entrypoint._sender(bot), pause_s=0.0, clock=clock, scope=store.scope)

    assert await delivery.tick() == 0

    assert len(session.calls) == 1, "второе сообщение того же клиента не отправлялось"
    assert store.blocked == {42: clock.now}
    assert [store.row(1).status, store.row(2).status] == ["cancelled", "cancelled"]


async def test_a_real_429_pauses_the_chain_for_the_requested_time() -> None:
    bot, session = scripted_bot(refusal(429, "Too Many Requests: retry after 30", retry_after=30))
    store, clock = Store([Row(1)]), Clock()
    delivery = Delivery(entrypoint._sender(bot), pause_s=0.0, clock=clock, scope=store.scope)

    await delivery.tick()
    clock.advance(seconds=29)
    await delivery.tick()
    assert len(session.calls) == 1, "до конца паузы Bot API не трогаем"

    clock.advance(seconds=2)
    assert await delivery.tick() == 1
    assert store.row(1).sent_at == clock.now
