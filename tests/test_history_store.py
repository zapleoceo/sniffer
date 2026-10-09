"""Отметка «сходили в чат» — без Postgres: проверяем, что и когда зовёт хранилище."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from sniffer.collector import history_store
from sniffer.collector.history_store import DatabaseHistoryStore
from sniffer.domain.records import Chat, RawMessage

CHAT = Chat(tg_id=-100123, username="quiet", title="Тихий чат", city="nha_trang", last_msg_id=500)


@dataclass
class Calls:
    synced: list[tuple[int, int]] = field(default_factory=list)
    committed: bool = False
    fail_insert_with: Exception | None = None


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    seen = Calls()

    @asynccontextmanager
    async def fake_session() -> AsyncIterator[object]:
        yield object()
        seen.committed = True

    class FakeChats:
        def __init__(self, _session: object) -> None: ...

        async def mark_synced(self, tg_id: int, last_msg_id: int) -> None:
            seen.synced.append((tg_id, last_msg_id))

    class FakeRaw:
        def __init__(self, _session: object) -> None: ...

        async def add_many(self, messages: Sequence[RawMessage]) -> list[RawMessage]:
            if seen.fail_insert_with is not None:
                raise seen.fail_insert_with
            return list(messages)

    patch: Any = monkeypatch
    patch.setattr(history_store, "_session", fake_session)
    patch.setattr(history_store, "ChatRepository", FakeChats)
    patch.setattr(history_store, "RawMessageRepository", FakeRaw)
    return seen


async def test_a_pass_without_new_messages_still_marks_the_chat_as_read(calls: Calls) -> None:
    """Тихая, но читаемая группа не должна выглядеть мёртвой на /database."""
    inserted = await DatabaseHistoryStore().store(CHAT, [], CHAT.last_msg_id)

    assert inserted == 0
    assert calls.synced == [(CHAT.tg_id, 500)], "курсор прежний, отметка времени обновлена"
    assert calls.committed


async def test_a_failed_pass_does_not_mark_the_chat(calls: Calls) -> None:
    calls.fail_insert_with = RuntimeError("insert failed")

    with pytest.raises(RuntimeError):
        await DatabaseHistoryStore().store(CHAT, [], CHAT.last_msg_id)

    assert calls.synced == []
    assert not calls.committed
