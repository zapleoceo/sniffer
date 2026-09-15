"""A completed collection task becomes one durable, user-visible answer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.agent_app import followup
from sniffer.agent_app.main import CatalogAnswer
from sniffer.db.repositories.collection_tasks import CollectionLease, CollectionRecipient
from sniffer.notifier.delivery import render
from sniffer.sources.base import RawItem

LEASE = CollectionLease(7, "lease", {}, 1, datetime.now(UTC) + timedelta(minutes=2))
RECIPIENTS = [CollectionRecipient(156, 21, 1), CollectionRecipient(157, 21, 1)]


@pytest.fixture
def repository(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    repo = SimpleNamespace(
        pending_recipients=AsyncMock(return_value=RECIPIENTS),
        queue_reply=AsyncMock(return_value=True),
    )
    session = SimpleNamespace(commit=AsyncMock())

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, session)

    monkeypatch.setattr(followup, "session_scope", sessions)
    monkeypatch.setattr(followup, "CollectionTaskRepository", lambda _: repo)
    return repo


async def test_one_agent_analysis_answers_every_subscriber_once(
    repository: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = RawItem(
        source="archive",
        external_id="9",
        url="https://t.me/example/9",
        title="Honda rental",
        price_raw="3 млн/месяц",
    )
    search = AsyncMock(return_value=CatalogAnswer([item]))
    monkeypatch.setattr(followup, "search_request", search)

    assert await followup.queue_answers(LEASE) == 2
    search.assert_awaited_once_with(156, 21, 1, allow_collection=False)
    assert repository.queue_reply.await_count == 2
    payload = repository.queue_reply.await_args_list[0].args[3]
    assert payload["collection_task_id"] == 7
    assert payload["request_id"] == 21
    message = render(payload)
    assert "Обновление каталога завершено" in message
    assert "Honda rental" in message and "3 млн/месяц" in message


async def test_empty_analysis_still_gives_a_terminal_answer(
    repository: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(followup, "search_request", AsyncMock(return_value=CatalogAnswer([])))

    assert await followup.queue_answers(LEASE) == 2
    payload = repository.queue_reply.await_args_list[0].args[3]
    assert "пока нет" in render(payload)


async def test_analysis_failure_is_an_answer_not_a_silent_task(
    repository: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        followup, "search_request", AsyncMock(side_effect=RuntimeError("private details"))
    )

    assert await followup.queue_answers(LEASE) == 2
    payload = repository.queue_reply.await_args_list[0].args[3]
    assert "проверить результаты не удалось" in render(payload).lower()
    assert "private details" not in render(payload)


async def test_collection_failure_checks_existing_catalogue_and_answers(
    repository: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(followup, "search_request", AsyncMock(return_value=CatalogAnswer([])))

    assert await followup.queue_failure_answers(LEASE) == 2
    payload = repository.queue_reply.await_args_list[0].args[3]
    message = render(payload)
    assert "Полностью обновить каталог не удалось" in message
    assert "пока нет" in message


async def test_budget_cap_answer_never_calls_the_agent(
    repository: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    search = AsyncMock()
    monkeypatch.setattr(followup, "search_request", search)

    assert await followup.queue_cap_answers(LEASE) == 2
    search.assert_not_awaited()
    payload = repository.queue_reply.await_args_list[0].args[3]
    assert "не удалось проверить" in render(payload)
