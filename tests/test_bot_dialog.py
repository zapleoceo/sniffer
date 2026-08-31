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
from sniffer.bot import journal
from sniffer.bot.handlers import search as handler
from sniffer.broker import usage
from sniffer.domain.passport import Passport
from sniffer.search.plan import SearchPlan, SearchTask
from sniffer.sources.base import RawItem

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


class FakeChat:
    id = 42


class FakeUser:
    id = 169510539
    username = "client"


class FakeMessage:
    """Ровно то, что хендлер трогает у сообщения."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.chat = FakeChat()
        self.from_user = FakeUser()
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs: Any) -> None:
        self.answers.append(text)


class FakeJournal:
    """Журнал без базы: запоминает, что бот записал бы о диалоге.

    Подменяется целиком, потому что настоящий пошёл бы в Postgres. Проверять
    здесь надо диалог, а запись в базу проверяют тесты репозиториев.
    """

    def __init__(self) -> None:
        self.opened: list[tuple[int, str]] = []
        self.answers: list[str] = []
        self.closed: list[dict[str, Any]] = []

    async def open_request(
        self, tg_user_id: int, text: str, *, username: str | None = None
    ) -> journal.OpenRequest:
        self.opened.append((tg_user_id, text))
        return journal.OpenRequest(user_id=1, request_id=len(self.opened))

    async def log_answer(self, opened: journal.OpenRequest | None, text: str) -> None:
        self.answers.append(text)

    async def close_request(self, opened: journal.OpenRequest | None, **kwargs: Any) -> None:
        self.closed.append({"request_id": None if opened is None else opened.request_id, **kwargs})


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
def offline(monkeypatch: pytest.MonkeyPatch) -> FakeJournal:
    """Ни модели, ни сети, ни базы: диалог обязан работать и без них."""
    monkeypatch.setattr(handler, "QueryIntake", FakeIntake)
    monkeypatch.setattr(handler, "SearchPlanner", FakePlanner)
    fake = FakeJournal()
    for name in ("open_request", "log_answer", "close_request"):
        monkeypatch.setattr(journal, name, getattr(fake, name))
    return fake


def message(text: str) -> Any:
    return FakeMessage(text)


async def test_start_explains_what_the_bot_does() -> None:
    hello = message("/start")

    await handler.start(cast(Message, hello))

    assert len(hello.answers) == 1
    assert "объявлени" in hello.answers[0]


async def test_query_turns_into_cards(
    offline: FakeJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    offline: FakeJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Требование verifier'а доезжает до клиента, а не остаётся в коде."""

    async def run_plan(_plan: SearchPlan, **_kwargs: Any) -> list[RawItem]:
        return [found("old", age_days=40)]

    monkeypatch.setattr(handler, "run_plan", run_plan)
    request = message("ищу скутер")

    await handler.search(cast(Message, request))

    assert "могло быть продано" in request.answers[1]


async def test_nothing_found_is_said_out_loud(
    offline: FakeJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run_plan(_plan: SearchPlan, **_kwargs: Any) -> list[RawItem]:
        return []

    monkeypatch.setattr(handler, "run_plan", run_plan)
    request = message("ищу вертолёт")

    await handler.search(cast(Message, request))

    assert request.answers[1] == handler.NOTHING_FOUND


async def test_broken_search_still_answers(
    offline: FakeJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Клиент не должен остаться без ответа из-за чужого сломанного API."""

    async def run_plan(_plan: SearchPlan, **_kwargs: Any) -> list[RawItem]:
        raise RuntimeError("источник отдал не то")

    monkeypatch.setattr(handler, "run_plan", run_plan)
    request = message("ищу скутер")

    await handler.search(cast(Message, request))

    assert request.answers[1] == handler.SEARCH_FAILED


async def test_empty_message_is_ignored(offline: FakeJournal) -> None:
    request = message("   ")

    await handler.search(cast(Message, request))

    assert request.answers == []


async def test_journal_records_the_whole_turn(
    offline: FakeJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Дашборд показывает и вопрос, и оба ответа, и время по этапам."""

    async def run_plan(_plan: SearchPlan, **_kwargs: Any) -> list[RawItem]:
        return [found("1")]

    monkeypatch.setattr(handler, "run_plan", run_plan)
    request = message("ищу скутер")

    await handler.search(cast(Message, request))

    assert offline.opened == [(FakeUser.id, "ищу скутер")]
    assert offline.answers == request.answers
    closed = offline.closed[0]
    assert closed["result_count"] == 1
    assert closed.get("error") is None
    assert set(closed["stages"]) == {"intake_ms", "plan_ms", "search_ms"}


async def test_failed_search_is_closed_with_the_reason(
    offline: FakeJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Упавший запрос обязан остаться в логе — иначе видно только удачные."""

    async def run_plan(_plan: SearchPlan, **_kwargs: Any) -> list[RawItem]:
        raise RuntimeError("источник отдал не то")

    monkeypatch.setattr(handler, "run_plan", run_plan)

    await handler.search(cast(Message, message("ищу скутер")))

    assert "RuntimeError" in offline.closed[0]["error"]


async def test_broker_calls_are_scoped_to_the_request(
    offline: FakeJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Расход, записанный во время поиска, принадлежит этому запросу."""
    seen: list[int | None] = []

    async def run_plan(_plan: SearchPlan, **_kwargs: Any) -> list[RawItem]:
        seen.append(usage.current_request_id())
        return []

    monkeypatch.setattr(handler, "run_plan", run_plan)

    await handler.search(cast(Message, message("ищу скутер")))

    assert seen == [1]
    # За пределами обработки сообщения область снимается: следующий вызов
    # брокера не должен приписаться прошлому клиенту.
    assert usage.current_request_id() is None


def test_dispatcher_knows_the_dialog() -> None:
    dispatcher = bot_app.build_dispatcher()

    assert [router.name for router in dispatcher.sub_routers] == ["search"]
