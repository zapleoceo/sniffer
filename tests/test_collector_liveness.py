"""Перечитывание известных объявлений чата: удалённое и «продано» гаснут."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

from sniffer.collector.liveness import IDS_PER_CALL, LivenessChecker
from sniffer.domain.records import Chat
from sniffer.sources.telegram_discover_reference import MessageLike


@dataclass(frozen=True)
class Message:
    id: int
    message: str | None
    entities: tuple[object, ...] = ()


@dataclass
class Reader:
    alive: dict[int, str]
    stale_usernames: set[str] = field(default_factory=set)
    calls: list[tuple[int | str, list[int]]] = field(default_factory=list)

    async def messages_by_ids(
        self, entity: int | str, ids: Sequence[int]
    ) -> Sequence[MessageLike | None]:
        self.calls.append((entity, list(ids)))
        if isinstance(entity, str) and entity in self.stale_usernames:
            raise ValueError(f'No user has "{entity}" as username')
        found = [Message(i, self.alive[i]) if i in self.alive else None for i in ids]
        return cast(Sequence[MessageLike | None], found)


@dataclass
class Store:
    chats: list[Chat]
    refs: dict[int, list[tuple[int, int]]]
    retired: list[int] = field(default_factory=list)

    async def active_chats(self, *, limit: int) -> list[Chat]:
        return self.chats[:limit]

    async def live_refs(self, chat: Chat, *, since: datetime) -> list[tuple[int, int]]:
        return self.refs.get(chat.tg_id, [])

    async def retire(self, listing_ids: list[int]) -> int:
        self.retired.extend(listing_ids)
        return len(listing_ids)


def chat(tg_id: int, username: str | None = None) -> Chat:
    return Chat(tg_id=tg_id, title=f"чат {tg_id}", city="nha_trang", username=username)


async def test_deleted_and_sold_posts_are_retired_live_ones_stay() -> None:
    reader = Reader(alive={11: "Продам Honda Lead, 12 млн", 13: "ПРОДАНО Honda Vision"})
    store = Store([chat(-1, "flea")], {-1: [(101, 11), (102, 12), (103, 13)]})

    assert await LivenessChecker(reader=reader, store=store).run() == 2

    assert sorted(store.retired) == [102, 103], "12 удалён, 13 исправлен в «продано»"


async def test_chats_are_checked_one_per_pass_in_a_circle() -> None:
    reader = Reader(alive={})
    store = Store([chat(-1, "a"), chat(-2, "b")], {-1: [(1, 1)], -2: [(2, 2)]})
    checker = LivenessChecker(reader=reader, store=store)

    for _ in range(3):
        await checker.run()

    assert [entity for entity, _ in reader.calls] == ["a", "b", "a"]


async def test_a_renamed_chat_is_checked_by_id() -> None:
    reader = Reader(alive={5: "Сдам байк"}, stale_usernames={"old"})
    store = Store([chat(-7, "old")], {-7: [(50, 5)]})

    await LivenessChecker(reader=reader, store=store).run()

    assert reader.calls == [("old", [5]), (-7, [5])]
    assert store.retired == []


async def test_ids_are_read_in_telegram_sized_batches() -> None:
    refs = [(n, n) for n in range(1, IDS_PER_CALL + 6)]
    reader = Reader(alive={n: "Продам" for n, _ in refs})
    store = Store([chat(-1, "flea")], {-1: refs})

    await LivenessChecker(reader=reader, store=store).run()

    assert [len(ids) for _, ids in reader.calls] == [IDS_PER_CALL, 5]


async def test_an_unreadable_chat_does_not_break_the_circle() -> None:
    class Broken(Reader):
        async def messages_by_ids(
            self, entity: int | str, ids: Sequence[int]
        ) -> Sequence[MessageLike | None]:
            raise ConnectionError("telegram down")

    store = Store([chat(-1)], {-1: [(1, 1)]})
    checker = LivenessChecker(reader=Broken(alive={}), store=store)

    assert await checker.run() == 0
    assert checker.position == 1
    assert store.retired == []
