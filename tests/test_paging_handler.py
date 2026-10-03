"""Кнопки «Ещё» и «Показать все» в хендлере: чужой и просроченный снимок, двойное нажатие."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup

from sniffer.bot import paging
from sniffer.bot.handlers import search as handler
from sniffer.bot.keyboards import PageCallback
from sniffer.bot.paging import MemorySnapshots, Snapshot
from sniffer.bot.quota import Account, QuotaService
from sniffer.simulation.ledger import MemoryLedger
from sniffer.sources.base import RawItem
from tests.quota_support import Clock, Slots

T0 = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)
TG = 42


class FakeUser:
    id = TG
    username = "dima"


class FakeMessage:
    def __init__(self, *, edit_fails: bool = False) -> None:
        self.from_user = FakeUser()
        self.answers: list[str] = []
        self.edited: list[Any] = []
        self.reply_markup = InlineKeyboardMarkup(inline_keyboard=[])
        self.edit_fails = edit_fails

    async def answer(self, text: str, **_kwargs: Any) -> None:
        self.answers.append(text)

    async def edit_reply_markup(self, reply_markup: Any = None) -> None:
        if self.edit_fails:
            raise TelegramBadRequest(method=cast(Any, None), message="message is not modified")
        self.edited.append(reply_markup)


class FakeCallback:
    def __init__(self, message: FakeMessage, user_id: int = TG) -> None:
        self.message = message
        self.from_user = FakeUser()
        self.from_user.id = user_id
        self.toasts: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.toasts.append((text, show_alert))


def lots(count: int) -> tuple[RawItem, ...]:
    return tuple(
        RawItem(
            source="archive",
            external_id=str(n),
            url=f"https://t.me/c/1/{n}",
            title=f"Lead {n}",
            raw={"listing_id": n},
        )
        for n in range(count)
    )


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> tuple[MemorySnapshots, str]:
    store = MemorySnapshots(clock=lambda: T0)
    token = store.put(Snapshot(owner=TG, items=lots(12), root=3, cursor=5))
    service = QuotaService(MemoryLedger(), entitlements=Slots(1), clock=Clock(T0))

    async def account_of(_client: Any) -> Account:
        return Account(1, TG)

    monkeypatch.setattr(handler, "Message", FakeMessage)
    monkeypatch.setattr(paging, "SNAPSHOTS", store)
    monkeypatch.setattr(handler, "quota", lambda: service)
    monkeypatch.setattr(handler, "account_of", account_of)
    return store, token


async def press(token: str, action: str, offset: int, **kwargs: Any) -> FakeCallback:
    callback = FakeCallback(FakeMessage(**kwargs))
    await handler.more(cast(Any, callback), PageCallback(token=token, action=action, offset=offset))
    return callback


async def test_a_press_shows_the_page_and_takes_the_buttons_off_the_old_one(
    wired: tuple[MemorySnapshots, str],
) -> None:
    _, token = wired
    callback = await press(token, "more", 5)
    message = callback.message
    assert callback.toasts == [(None, False)]
    assert len(message.answers) == 1 and "Карточки 6–10 из 12" in message.answers[0]
    assert len(message.edited) == 1


async def test_an_unknown_or_expired_snapshot_is_an_alert_and_shows_nothing(
    wired: tuple[MemorySnapshots, str],
) -> None:
    callback = await press("нет-такого", "more", 5)
    assert callback.toasts == [(paging.EXPIRED, True)]
    assert callback.message.answers == []


async def test_a_snapshot_of_another_user_is_refused_like_an_expired_one(
    wired: tuple[MemorySnapshots, str],
) -> None:
    _, token = wired
    callback = FakeCallback(FakeMessage(), user_id=999)
    await handler.more(cast(Any, callback), PageCallback(token=token, action="more", offset=5))
    assert callback.toasts == [(paging.EXPIRED, True)]
    assert callback.message.answers == []


async def test_a_repeated_press_gets_a_quiet_toast_and_no_second_page(
    wired: tuple[MemorySnapshots, str],
) -> None:
    _, token = wired
    await press(token, "more", 5)
    second = await press(token, "more", 5)
    assert second.toasts == [(paging.ALREADY_SHOWN, False)]
    assert second.message.answers == []


async def test_a_message_that_cannot_be_edited_does_not_spoil_the_shown_page(
    wired: tuple[MemorySnapshots, str],
) -> None:
    _, token = wired
    callback = await press(token, "all", 5, edit_fails=True)
    assert callback.message.answers, "карточки уже показаны"
