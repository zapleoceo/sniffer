"""Первый диалог целиком: текст клиента → паспорт → план → карточки.

Telegram и сеть заменены заглушками: проверяется поведение бота, а не работа
Bot API. Важнее всего два ответа подряд — «понял, ищу» и выдача: молчащий
минуту бот выглядит сломанным даже когда всё работает.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from aiogram.types import Message

from sniffer.bot import app as bot_app
from sniffer.bot.handlers import search as handler
from sniffer.domain.passport import Passport
from sniffer.search.plan import SearchPlan, SearchTask
from sniffer.sources.base import RawItem

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


class FakeChat:
    id = 42


class FakeMessage:
    """Ровно то, что хендлер трогает у сообщения."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.chat = FakeChat()
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs: Any) -> None:
        self.answers.append(text)


class FakeIntake:
    def __init__(self, _broker: Any = None) -> None:
        self.parsed: list[str] = []

    async def parse(self, text: str) -> Passport:
        self.parsed.append(text)
        return Passport(raw_query=text)


class FakePlanner:
    def __init__(self, _broker: Any = None) -> None:
        pass

    async def plan(self, _passport: Passport, sources: list[str]) -> SearchPlan:
        return SearchPlan(tasks=[SearchTask(source=sources[0], query="скутер")])


def found(external_id: str, *, age_days: int = 1) -> RawItem:
    return RawItem(
        source="chotot",
        external_id=external_id,
        url=f"https://www.chotot.com/{external_id}.htm",
        title=f"Honda Vision {external_id}",
        price_raw="25.000.000 đ",
        posted_at=NOW - timedelta(days=age_days),
    )


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ни модели, ни сети: диалог обязан работать и без них."""
    monkeypatch.setattr(handler, "QueryIntake", FakeIntake)
    monkeypatch.setattr(handler, "SearchPlanner", FakePlanner)


def message(text: str) -> Any:
    return FakeMessage(text)


async def test_start_explains_what_the_bot_does() -> None:
    hello = message("/start")

    await handler.start(cast(Message, hello))

    assert len(hello.answers) == 1
    assert "объявлени" in hello.answers[0]


async def test_query_turns_into_cards(offline: None, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run_plan(_plan: SearchPlan, **_kwargs: Any) -> list[RawItem]:
        return [found(str(index), age_days=index + 1) for index in range(8)]

    monkeypatch.setattr(handler, "run_plan", run_plan)
    request = message("ищу скутер в Нячанге до 400 долларов")

    await handler.search(cast(Message, request))

    assert len(request.answers) == 2
    assert "Ищу" in request.answers[0]
    cards = request.answers[1]
    # Пять карточек максимум, у каждой ссылка на оригинал.
    assert cards.count("открыть оригинал") == 5
    assert "https://www.chotot.com/0.htm" in cards


async def test_old_lot_is_marked_in_the_answer(
    offline: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Требование verifier'а доезжает до клиента, а не остаётся в коде."""

    async def run_plan(_plan: SearchPlan, **_kwargs: Any) -> list[RawItem]:
        return [found("old", age_days=40)]

    monkeypatch.setattr(handler, "run_plan", run_plan)
    request = message("ищу скутер")

    await handler.search(cast(Message, request))

    assert "могло быть продано" in request.answers[1]


async def test_nothing_found_is_said_out_loud(
    offline: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run_plan(_plan: SearchPlan, **_kwargs: Any) -> list[RawItem]:
        return []

    monkeypatch.setattr(handler, "run_plan", run_plan)
    request = message("ищу вертолёт")

    await handler.search(cast(Message, request))

    assert request.answers[1] == handler.NOTHING_FOUND


async def test_broken_search_still_answers(offline: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Клиент не должен остаться без ответа из-за чужого сломанного API."""

    async def run_plan(_plan: SearchPlan, **_kwargs: Any) -> list[RawItem]:
        raise RuntimeError("источник отдал не то")

    monkeypatch.setattr(handler, "run_plan", run_plan)
    request = message("ищу скутер")

    await handler.search(cast(Message, request))

    assert request.answers[1] == handler.SEARCH_FAILED


async def test_empty_message_is_ignored(offline: None) -> None:
    request = message("   ")

    await handler.search(cast(Message, request))

    assert request.answers == []


def test_dispatcher_knows_the_dialog() -> None:
    dispatcher = bot_app.build_dispatcher()

    assert [router.name for router in dispatcher.sub_routers] == ["search"]
