"""Добор архива чата: страницы вниз, потолок на проход, остановка на ошибке."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sniffer.collector.backfill import HistoryBackfill
from sniffer.domain.records import Chat, RawMessage

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@dataclass
class Msg:
    id: int
    message: str = "Продам байк"
    date: datetime = NOW
    media: None = None


@dataclass
class FakeReader:
    """Лента с известным содержимым. Читается вниз от `max_id`."""

    ids: list[int]
    calls: list[tuple[int, int]] = field(default_factory=list)
    boom: Exception | None = None
    fail_after: int = 10**6

    async def history(
        self, entity: int | str, *, limit: int, min_id: int = 0, max_id: int = 0
    ) -> list[Msg]:
        self.calls.append((max_id, limit))
        if self.boom is not None and len(self.calls) > self.fail_after:
            raise self.boom
        older = [number for number in self.ids if number < max_id]
        return [Msg(number) for number in sorted(older, reverse=True)[:limit]]


@dataclass
class FakeStore:
    chat: Chat | None
    saved: list[tuple[int, bool, int]] = field(default_factory=list)

    async def next_backfill(self) -> Chat | None:
        return self.chat

    async def store_archive(
        self, chat: Chat, messages: list[RawMessage], *, oldest_msg_id: int, done: bool
    ) -> int:
        self.saved.append((oldest_msg_id, done, len(messages)))
        return len(messages)


def a_chat(*, last: int = 100, backfill: int = 0) -> Chat:
    return Chat(
        tg_id=-100123,
        title="Барахолка",
        city="nha_trang",
        username="flea",
        last_msg_id=last,
        backfill_msg_id=backfill,
    )


def backfill(reader: FakeReader, store: FakeStore, **kwargs: object) -> HistoryBackfill:
    return HistoryBackfill(reader=reader, store=store, pause_s=0.0, **kwargs)  # type: ignore[arg-type]


async def test_the_archive_is_read_downwards_page_by_page() -> None:
    """Каждая следующая страница начинается там, где кончилась предыдущая."""
    reader = FakeReader(ids=list(range(1, 101)))
    store = FakeStore(a_chat(last=100))

    inserted = await backfill(reader, store, pages=3, page_size=10).run()

    assert [call[0] for call in reader.calls] == [100, 90, 80], "курсор обязан идти вниз"
    assert [saved[0] for saved in store.saved] == [90, 80, 70]
    assert inserted == 30


async def test_a_page_cap_stops_the_pass_without_finishing_the_archive() -> None:
    """Потолок на проход — то, что отличает добор от выкачки."""
    reader = FakeReader(ids=list(range(1, 10_001)))
    store = FakeStore(a_chat(last=10_000))

    await backfill(reader, store, pages=2, page_size=200).run()

    assert len(reader.calls) == 2, "проход обязан кончиться на потолке, а не на конце ленты"
    assert all(not saved[1] for saved in store.saved), "архив не дочитан — метки done быть не может"


async def test_reaching_the_start_of_the_chat_closes_it_forever() -> None:
    """Пустая страница означает начало ленты: больше вниз не ходим никогда."""
    reader = FakeReader(ids=[8, 9, 10])
    store = FakeStore(a_chat(last=10))

    await backfill(reader, store, pages=5, page_size=10).run()

    assert store.saved[-1][1] is True, "дочитали до начала — обязаны это запомнить"
    assert len(reader.calls) == 2, "после пустой страницы запросов быть не должно"


async def test_an_error_stops_the_pass_and_keeps_the_cursor_where_it_worked() -> None:
    """Курсор не прыгает через непрочитанное: следующий проход продолжит с места.

    Разбирать типы ошибок здесь незачем — «остановиться и прийти через
    пятнадцать минут» верный ответ на любую, включая ту, которой никто не
    назвал (CLAUDE.md о закрытых наборах исходов).
    """
    reader = FakeReader(ids=list(range(1, 101)), boom=ConnectionError("оборвалось"), fail_after=2)
    store = FakeStore(a_chat(last=100))

    inserted = await backfill(reader, store, pages=5, page_size=10).run()

    assert inserted == 20, "две удавшиеся страницы обязаны сохраниться"
    assert store.saved[-1] == (80, False, 10), "курсор остался на последней удавшейся"


async def test_an_interrupted_pass_resumes_from_the_stored_cursor() -> None:
    reader = FakeReader(ids=list(range(1, 101)))
    store = FakeStore(a_chat(last=100, backfill=50))

    await backfill(reader, store, pages=1, page_size=10).run()

    assert reader.calls[0][0] == 50, "продолжаем с сохранённого курсора, а не сверху"


async def test_a_chat_nobody_has_read_yet_is_left_to_the_forward_sync() -> None:
    """Добирать не от чего, пока догон свежего не поставил точку отсчёта."""
    reader = FakeReader(ids=[1, 2, 3])
    store = FakeStore(a_chat(last=0))

    assert await backfill(reader, store).run() == 0
    assert reader.calls == []


async def test_nothing_to_backfill_costs_no_requests() -> None:
    reader = FakeReader(ids=[1, 2, 3])
    assert await backfill(reader, FakeStore(None)).run() == 0
    assert reader.calls == []
