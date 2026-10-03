"""Клиент заблокировал бота или снял блокировку: апдейты настоящими типами aiogram.

Апдейты — настоящие `Update.model_validate(json)` через `Dispatcher.feed_update`,
бот — настоящий `Bot` с подставной сессией (`tests/bot_api_support.py`). Подделан
один шов: запись в базу (`reachability.record`), чтобы проверять, кого и как
помечает хендлер, а не SQL (его проверяют `test_notifier_queries.py` и CI на Postgres).

Роутеры у `bot` — объекты уровня модуля, и ко второму диспетчеру в том же процессе
aiogram их не присоединит («Router is already attached»), поэтому фикстура на время
теста снимает прежнюю привязку.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import (
    ChatMemberAdministrator,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberOwner,
    ChatMemberRestricted,
    Update,
)
from pydantic import BaseModel

from sniffer.bot import reachability
from sniffer.bot.app import build_dispatcher
from sniffer.bot.handlers import membership, search
from tests.bot_api_support import scripted_bot

CLIENT = 42
BOT_ID = 999
WHEN = 1_760_000_000

Calls = list[tuple[int, bool, datetime]]


@pytest.fixture
def dispatcher(monkeypatch: pytest.MonkeyPatch) -> Dispatcher:
    for router in (search.router, membership.router):
        monkeypatch.setattr(router, "_parent_router", None)
    built: Dispatcher = build_dispatcher()
    return built


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Calls:
    calls: Calls = []

    async def record(tg_user_id: int, *, blocked: bool, at: datetime) -> None:
        calls.append((tg_user_id, blocked, at))

    monkeypatch.setattr(reachability, "record", record)
    return calls


BOT_USER = {"id": BOT_ID, "is_bot": True, "first_name": "RecVN"}
MEMBER_MODELS: dict[str, type[BaseModel]] = {
    "member": ChatMemberMember,
    "kicked": ChatMemberBanned,
    "left": ChatMemberLeft,
    "administrator": ChatMemberAdministrator,
    "restricted": ChatMemberRestricted,
    "creator": ChatMemberOwner,
}


def chat_member(status: str) -> dict[str, Any]:
    """Участник так, как его присылает Telegram: у каждого статуса свои обязательные поля."""
    payload: dict[str, Any] = {"user": BOT_USER, "status": status}
    for name, field in MEMBER_MODELS[status].model_fields.items():
        if field.is_required() and name not in payload:
            payload[name] = 0 if name == "until_date" else True
    return payload


def membership_update(status: str, *, chat: str = "private", old: str = "member") -> Update:
    """`my_chat_member` так, как его присылает Telegram: статус бота изменился."""
    place: dict[str, Any] = {"id": CLIENT, "type": chat}
    place.update({"first_name": "Дима"} if chat == "private" else {"title": "Чат"})
    return Update.model_validate(
        {
            "update_id": 1,
            "my_chat_member": {
                "chat": place,
                "from": {"id": CLIENT, "is_bot": False, "first_name": "Дима"},
                "date": WHEN,
                "old_chat_member": chat_member(old),
                "new_chat_member": chat_member(status),
            },
        }
    )


def message_update(text: str = "/start", *, sender: bool = True) -> Update:
    message: dict[str, Any] = {
        "message_id": 7,
        "date": WHEN,
        "chat": {"id": CLIENT, "type": "private", "first_name": "Дима"},
        "text": text,
        "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}],
    }
    if sender:
        message["from"] = {"id": CLIENT, "is_bot": False, "first_name": "Дима"}
    return Update.model_validate({"update_id": 2, "message": message})


async def feed(dispatcher: Dispatcher, update: Update) -> list[SendMessage]:
    bot, session = scripted_bot()
    await dispatcher.feed_update(bot, update)
    return [call for call in session.calls if isinstance(call, SendMessage)]


# ── Telegram должен присылать эти апдейты ───────────────────────────────────


def test_the_dispatcher_asks_telegram_for_my_chat_member_updates(dispatcher: Dispatcher) -> None:
    """Без явной подписки aiogram просит у Telegram только типы своих хендлеров."""
    types = set(dispatcher.resolve_used_update_types())

    assert {"my_chat_member", "message", "callback_query", "pre_checkout_query"} <= types


# ── блокировка и снятие ─────────────────────────────────────────────────────


async def test_blocking_the_bot_marks_the_client_at_the_moment_telegram_names(
    dispatcher: Dispatcher, recorded: Calls
) -> None:
    await feed(dispatcher, membership_update("kicked"))

    assert recorded == [(CLIENT, True, datetime.fromtimestamp(WHEN, UTC))]


async def test_unblocking_the_bot_lifts_the_mark(dispatcher: Dispatcher, recorded: Calls) -> None:
    await feed(dispatcher, membership_update("member", old="kicked"))

    assert [(user, blocked) for user, blocked, _ in recorded] == [(CLIENT, False)]


@pytest.mark.parametrize("status", ["left", "administrator", "restricted", "creator"])
async def test_other_statuses_of_the_bot_in_a_private_chat_change_nothing(
    dispatcher: Dispatcher, recorded: Calls, status: str
) -> None:
    """Блокировка — это `kicked`; прочее в личном чате не бывает, но и считать им нечего."""
    await feed(dispatcher, membership_update(status))

    assert recorded == []


@pytest.mark.parametrize("chat", ["group", "supergroup", "channel"])
async def test_the_bot_being_removed_from_a_group_does_not_block_the_person_who_added_it(
    dispatcher: Dispatcher, recorded: Calls, chat: str
) -> None:
    await feed(dispatcher, membership_update("kicked", chat=chat))

    assert recorded == [], "человека пометили заблокировавшим из-за чата, в который он добавил бота"


async def test_a_block_is_not_undone_by_the_update_that_reported_it(
    dispatcher: Dispatcher, recorded: Calls
) -> None:
    """Апдейт о блокировке тоже «от клиента»: middleware на нём снял бы метку сразу."""
    await feed(dispatcher, membership_update("kicked"))

    assert [blocked for _, blocked, _ in recorded] == [True], "после блокировки пошло снятие"


# ── любое сообщение клиента доказывает, что бот ему доступен ────────────────


async def test_a_message_from_the_client_lifts_a_stale_block_and_is_still_answered(
    dispatcher: Dispatcher, recorded: Calls
) -> None:
    """Потерянный апдейт `member` не должен оставлять платящего клиента без слежения навсегда."""
    answers = await feed(dispatcher, message_update("/start"))

    assert [(user, blocked) for user, blocked, _ in recorded] == [(CLIENT, False)]
    assert len(answers) == 1, "диалог обязан отработать как раньше"


async def test_a_message_without_an_author_marks_nobody(
    dispatcher: Dispatcher, recorded: Calls
) -> None:
    """Пост от имени канала: клиента нет, и снимать метку не с кого."""
    await feed(dispatcher, message_update("/start", sender=False))

    assert recorded == []


def test_messages_clicks_and_payment_checks_all_carry_the_lifting_middleware(
    dispatcher: Dispatcher,
) -> None:
    observers = (dispatcher.message, dispatcher.callback_query, dispatcher.pre_checkout_query)

    for observer in observers:
        assert any(isinstance(m, reachability.MarkReachable) for m in observer.outer_middleware)


def test_the_membership_updates_do_not_carry_the_lifting_middleware(dispatcher: Dispatcher) -> None:
    assert not any(
        isinstance(m, reachability.MarkReachable)
        for m in dispatcher.my_chat_member.outer_middleware
    )
    assert not any(
        isinstance(m, reachability.MarkReachable) for m in dispatcher.update.outer_middleware
    )


# ── запись в базу ───────────────────────────────────────────────────────────


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def fake_repository(changed: int | None) -> Callable[[Any], Any]:
    class Repository:
        def __init__(self, _session: Any) -> None:
            return None

        async def set_bot_blocked(
            self, tg_user_id: int, *, blocked: bool, at: datetime
        ) -> int | None:
            return changed

    return Repository


@pytest.mark.parametrize(("changed", "commits"), [(5, 1), (None, 0)])
async def test_the_record_commits_only_when_something_changed(
    monkeypatch: pytest.MonkeyPatch, changed: int | None, commits: int
) -> None:
    """На каждое сообщение клиента — один запрос без записи и без коммита."""
    session = FakeSession()

    class Scope:
        async def __aenter__(self) -> FakeSession:
            return session

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(reachability, "session_scope", lambda: Scope())
    monkeypatch.setattr(reachability, "UserRepository", fake_repository(changed))

    await reachability.record(CLIENT, blocked=False, at=datetime.now(UTC))

    assert session.commits == commits
