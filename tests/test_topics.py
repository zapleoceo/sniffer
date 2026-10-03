"""Темы Telegram: выключатель, самопроверка при старте, откуда берётся тема клиента."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import CreateForumTopic, GetMe
from aiogram.types import CallbackQuery, Chat, Message, User

from sniffer.bot import tab_flow as tab_flow
from sniffer.bot import topics as topics
from sniffer.bot import watch_flow as watch_flow
from sniffer.bot.store import Client
from sniffer.config import Settings
from sniffer.domain.passport import Category, Passport
from sniffer.domain.records import QueryOverview


@pytest.fixture(autouse=True)
def reset_mode() -> Any:
    topics.set_active(False)
    yield
    topics.set_active(False)


class FakeBot:
    def __init__(self, *, has_topics: bool = True, error: BaseException | None = None) -> None:
        self.has_topics, self.error = has_topics, error
        self.created: list[tuple[int, str]] = []
        self.renamed: list[tuple[int, int, str]] = []

    async def get_me(self) -> SimpleNamespace:
        if self.error is not None:
            raise self.error
        return SimpleNamespace(has_topics_enabled=self.has_topics)

    async def create_forum_topic(self, *, chat_id: int, name: str) -> SimpleNamespace:
        if self.error is not None:
            raise self.error
        self.created.append((chat_id, name))
        return SimpleNamespace(message_thread_id=900 + len(self.created))

    async def edit_forum_topic(self, *, chat_id: int, message_thread_id: int, name: str) -> None:
        self.renamed.append((chat_id, message_thread_id, name))


def as_bot(fake: FakeBot) -> Bot:
    return cast(Bot, fake)


def message(*, thread: int | None, is_topic: bool | None) -> Message:
    return Message.model_construct(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=9, type="private"),
        from_user=User(id=9, is_bot=False, first_name="x", username="u"),
        message_thread_id=thread,
        is_topic_message=is_topic,
    )


def network_error() -> TelegramNetworkError:
    return TelegramNetworkError(GetMe(), "down")


async def test_the_flag_off_means_no_topics_even_if_botfather_has_them() -> None:
    bot = FakeBot(has_topics=True)
    assert await topics.check(as_bot(bot), Settings(topics_enabled=False)) is False
    assert not topics.active()


async def test_the_flag_on_and_botfather_on_activates_topics() -> None:
    assert await topics.check(as_bot(FakeBot()), Settings(topics_enabled=True)) is True
    assert topics.active()


async def test_the_flag_on_but_botfather_off_stays_without_topics() -> None:
    bot = FakeBot(has_topics=False)
    assert await topics.check(as_bot(bot), Settings(topics_enabled=True)) is False
    assert not topics.active()


async def test_a_failed_self_check_degrades_instead_of_crashing_the_bot() -> None:
    bot = FakeBot(error=network_error())
    assert await topics.check(as_bot(bot), Settings(topics_enabled=True)) is False
    assert not topics.active()


async def test_the_flag_defaults_to_off() -> None:
    assert Settings().topics_enabled is False


def test_a_thread_is_used_only_when_topics_are_active_and_the_message_is_a_topic_one() -> None:
    topics.set_active(True)
    assert topics.thread_of(message(thread=77, is_topic=True)) == 77
    assert topics.thread_of(message(thread=None, is_topic=None)) is None
    # Ответ-цитата в обычном чате тоже несёт message_thread_id — это не тема.
    assert topics.thread_of(message(thread=77, is_topic=None)) is None
    assert topics.thread_of(message(thread=77, is_topic=False)) is None


def test_with_topics_off_the_thread_is_ignored() -> None:
    assert topics.thread_of(message(thread=77, is_topic=True)) is None


def test_the_client_carries_the_thread() -> None:
    topics.set_active(True)
    client = topics.client_of_message(message(thread=77, is_topic=True))
    assert client == Client(9, "u", 77)
    assert topics.client_of_message(Message.model_construct(from_user=None)) is None


def test_a_callback_takes_the_thread_from_the_message_it_was_pressed_under() -> None:
    topics.set_active(True)
    query = CallbackQuery.model_construct(
        id="c", from_user=User(id=9, is_bot=False, first_name="x", username="u")
    )
    assert topics.client_of_callback(query, message(thread=5, is_topic=True)).thread_id == 5
    assert topics.client_of_callback(query, message(thread=None, is_topic=None)).thread_id is None


async def test_a_blank_topic_is_created_for_the_new_search() -> None:
    bot = FakeBot()
    assert await tab_flow.create_blank(as_bot(bot), Client(9))
    assert bot.created == [(9, tab_flow.NEW_TOPIC_NAME)]


async def test_a_failed_topic_creation_is_reported_not_raised() -> None:
    bot = FakeBot(
        error=TelegramBadRequest(
            CreateForumTopic(chat_id=1, name="x"), "Bad Request: not enough rights"
        )
    )
    assert not await tab_flow.create_blank(as_bot(bot), Client(9))


async def test_opening_a_tab_for_a_missing_search_creates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def nothing(client: Client, root: int) -> None:
        return None

    monkeypatch.setattr(watch_flow, "overview", nothing)
    bot = FakeBot()
    assert await tab_flow.open_tab(as_bot(bot), Client(9), 5) is None
    assert bot.created == []


async def test_a_failed_tab_creation_returns_none_before_touching_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def found(client: Client, root: int) -> QueryOverview:
        return QueryOverview(root=5, passport=Passport(category=Category.MOTORBIKE, city="x"))

    monkeypatch.setattr(watch_flow, "overview", found)
    bot = FakeBot(error=network_error())
    assert await tab_flow.open_tab(as_bot(bot), Client(9), 5) is None


async def test_the_title_sync_does_nothing_outside_a_thread() -> None:
    bot = FakeBot()
    await tab_flow.sync_title(as_bot(bot), Client(9))
    assert bot.renamed == []
