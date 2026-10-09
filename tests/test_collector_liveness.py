"""Перечитывание известных объявлений чата: удалённое и «продано» гаснут."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

from structlog.testing import capture_logs

from sniffer.collector.liveness import ALL_GONE_THRESHOLD, IDS_PER_CALL, LivenessChecker
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
    unresolved_ids: set[int] = field(default_factory=set)
    calls: list[tuple[int | str, list[int]]] = field(default_factory=list)

    async def messages_by_ids(
        self, entity: int | str, ids: Sequence[int]
    ) -> Sequence[MessageLike | None]:
        self.calls.append((entity, list(ids)))
        if isinstance(entity, str) and entity in self.stale_usernames:
            raise ValueError(f'No user has "{entity}" as username')
        if isinstance(entity, int) and entity in self.unresolved_ids:
            raise ValueError(f"Could not find the input entity for {entity}")
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


async def test_every_post_gone_at_once_is_a_read_failure_not_a_cleanup() -> None:
    """Все номера чата вернули `None` — это сбой доступа, снимать нечего."""
    refs = [(100 + n, n) for n in range(1, ALL_GONE_THRESHOLD + 1)]
    store = Store([chat(-1, "flea")], {-1: refs})

    with capture_logs() as logs:
        retired = await LivenessChecker(reader=Reader(alive={}), store=store).run()

    assert retired == 0
    assert store.retired == []
    warnings = [entry for entry in logs if entry["event"] == "collector.liveness_all_gone"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning" and warnings[0]["checked"] == len(refs)


async def test_every_post_gone_across_several_batches_is_still_a_failure() -> None:
    refs = [(n, n) for n in range(1, IDS_PER_CALL * 2 + 3)]
    store = Store([chat(-1, "flea")], {-1: refs})

    assert await LivenessChecker(reader=Reader(alive={}), store=store).run() == 0
    assert store.retired == []


async def test_a_few_posts_all_gone_is_below_the_failure_threshold() -> None:
    """Чат с тремя карточками честно может потерять все три."""
    refs = [(100 + n, n) for n in range(1, ALL_GONE_THRESHOLD)]
    store = Store([chat(-1, "flea")], {-1: refs})

    assert await LivenessChecker(reader=Reader(alive={}), store=store).run() == len(refs)
    assert sorted(store.retired) == [listing for listing, _ in refs]


async def test_a_partial_answer_retires_the_gone_ones_as_before() -> None:
    refs = [(100 + n, n) for n in range(1, ALL_GONE_THRESHOLD + 3)]
    reader = Reader(alive={1: "Продам Honda Lead, 12 млн"})
    store = Store([chat(-1, "flea")], {-1: refs})

    assert await LivenessChecker(reader=reader, store=store).run() == len(refs) - 1
    assert 101 not in store.retired


async def test_chats_are_checked_one_per_pass_in_a_circle() -> None:
    reader = Reader(alive={})
    store = Store([chat(-1, "a"), chat(-2, "b")], {-1: [(1, 1)], -2: [(2, 2)]})
    checker = LivenessChecker(reader=reader, store=store)

    for _ in range(3):
        await checker.run()

    assert [entity for entity, _ in reader.calls] == [-1, -2, -1]


async def test_chats_are_read_by_id_first_without_resolving_the_name() -> None:
    """Имя стоит `ResolveUsername` со своим флуд-лимитом; id уже в кэше клиента."""
    reader = Reader(alive={5: "Сдам байк"}, stale_usernames={"old"})
    store = Store([chat(-7, "old")], {-7: [(50, 5)]})

    await LivenessChecker(reader=reader, store=store).run()

    assert reader.calls == [(-7, [5])]
    assert store.retired == []


async def test_an_unresolved_id_falls_back_to_the_username() -> None:
    reader = Reader(alive={5: "Сдам байк"}, unresolved_ids={-7})
    store = Store([chat(-7, "flea")], {-7: [(50, 5)]})

    await LivenessChecker(reader=reader, store=store).run()

    assert reader.calls == [(-7, [5]), ("flea", [5])]
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
