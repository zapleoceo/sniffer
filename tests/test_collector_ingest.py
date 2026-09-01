"""Догон истории чатов: без сети и без настоящего Telegram."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

from sniffer.collector.ingest import HistorySyncer
from sniffer.domain.records import Chat, RawMessage
from sniffer.sources.telegram_discover_reference import MessageLike


@dataclass(frozen=True)
class FakeMessage:
    id: int
    message: str | None
    date: datetime | None
    media: object | None = None
    entities: tuple[object, ...] = ()


@dataclass
class FakeReader:
    messages: list[FakeMessage]
    calls: list[tuple[int | str, int, int]] = field(default_factory=list)

    async def history(self, entity: int | str, *, limit: int, min_id: int) -> Sequence[MessageLike]:
        self.calls.append((entity, limit, min_id))
        return cast(Sequence[MessageLike], self.messages)


@dataclass
class FakeStore:
    chats: list[Chat]
    saved: list[tuple[Chat, list[RawMessage], int]] = field(default_factory=list)

    async def active_chats(self, *, limit: int) -> list[Chat]:
        return self.chats[:limit]

    async def store(self, chat: Chat, messages: Sequence[RawMessage], cursor: int) -> int:
        self.saved.append((chat, list(messages), cursor))
        return len(messages)


async def test_history_is_deduplicable_stored_before_cursor_and_feeds_cross_links() -> None:
    chat = Chat(
        tg_id=-10042, username="nha_flea", title="Барахолка", city="nha_trang", last_msg_id=10
    )
    reader = FakeReader(
        [
            FakeMessage(11, "Продам Honda Vision", datetime(2026, 9, 1, tzinfo=UTC)),
            FakeMessage(12, None, datetime(2026, 9, 1, tzinfo=UTC)),
            FakeMessage(13, "Наш второй чат: t.me/nha_second", datetime(2026, 9, 1, tzinfo=UTC)),
        ]
    )
    store = FakeStore([chat])
    harvested: list[tuple[int, str]] = []

    async def discover(messages: Sequence[MessageLike], found_in: str) -> int:
        harvested.append((len(messages), found_in))
        return 1

    synced = await HistorySyncer(reader=reader, store=store, discover=discover).sync()

    assert synced == 2
    assert reader.calls == [("nha_flea", 200, 10)]
    assert len(store.saved) == 1
    _, raw, cursor = store.saved[0]
    assert [message.msg_id for message in raw] == [11, 13]
    assert cursor == 13, "курсор двигается и через пустое сервисное сообщение"
    assert raw[0].text_hash != raw[1].text_hash
    assert harvested == [(3, "nha_flea")]
