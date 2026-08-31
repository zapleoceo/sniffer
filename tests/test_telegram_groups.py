"""Адаптер telegram_groups на зафиксированной выдаче messages.search.

Сети здесь нет и быть не должно: Telethon подменён фейком, который знает ровно
разрешённые методы и падает на любом другом обращении. Тест, который ходит в
Telegram, проверяет не адаптер, а связь — и заодно тратит лимиты аккаунта,
ради сохранности которого весь этот адаптер и написан так осторожно.

Фикстура `fixtures/telegram_group_messages.json` — сообщения в том объёме,
который читает адаптер, с крайними случаями: пост без текста, пост без даты,
чат без username.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from telethon.errors import FloodWaitError

from sniffer.config import Settings
from sniffer.sources import telegram_client, telegram_groups, telegram_reference
from sniffer.sources.base import get_source, registered_sources
from sniffer.sources.telegram_groups import TelegramGroupsSource
from sniffer.sources.telegram_reference import (
    MAX_CHATS_PER_SEARCH,
    SOURCE_NAME,
    ChatRef,
    internal_chat_id,
    message_link,
)

FIXTURE = Path(__file__).parent / "fixtures" / "telegram_group_messages.json"

# Методы, которых у юзербота быть не должно (spec-v2, 6.1). Ищем именно вызов,
# а не упоминание: имена методов в документации запрещать глупо.
OUTGOING_CALL = re.compile(
    r"\.(send_\w+|forward_\w+|delete_\w+|edit_\w+|join_\w+|mark_read|pin_\w+|"
    r"iter_dialogs|get_dialogs)\s*\("
)


@dataclass(frozen=True, slots=True)
class FakeMessage:
    """Сообщение Telethon в объёме протокола `MessageLike`."""

    id: int
    message: str | None
    date: datetime | None
    media: object | None = None


@dataclass
class FakeTelegram:
    """Telethon с разрешённой поверхностью и без сети.

    Любое обращение помимо `connect` / `disconnect` / `get_messages` — падение
    теста, а не тихая заглушка: молчаливый фейк пропустил бы ровно тот дефект,
    от которого зависит жизнь аккаунта.
    """

    replies: dict[object, list[FakeMessage]] = field(default_factory=dict)
    floods: list[int] = field(default_factory=list)
    fails: set[object] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)
    queried: list[object] = field(default_factory=list)
    limits: list[int] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    async def connect(self) -> None:
        self.calls.append("connect")

    async def disconnect(self) -> None:
        self.calls.append("disconnect")

    async def get_messages(
        self,
        entity: int | str,
        *,
        search: str,
        limit: int,
    ) -> Sequence[FakeMessage]:
        self.calls.append("get_messages")
        self.queried.append(entity)
        self.limits.append(limit)
        self.events.append(f"start:{entity}")
        # Точка, в которой параллельный обход выдал бы себя чередованием.
        await asyncio.sleep(0)
        if self.floods and (seconds := self.floods.pop(0)):
            raise FloodWaitError(request=None, capture=seconds)
        if entity in self.fails:
            raise ValueError(f"чат {entity} недоступен")
        self.events.append(f"end:{entity}")
        return self.replies.get(entity, [])

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"юзербот дёрнул запрещённый метод Telegram: {name}")


@dataclass
class FakeDirectory:
    """Реестр чатов без БД: тот же протокол, другая реализация."""

    chats: Sequence[ChatRef]
    asked: list[tuple[str, int]] = field(default_factory=list)

    async def active_chats(self, city: str, limit: int) -> Sequence[ChatRef]:
        self.asked.append((city, limit))
        return self.chats


class BrokenDirectory:
    async def active_chats(self, city: str, limit: int) -> Sequence[ChatRef]:
        raise RuntimeError("нет соединения с базой")


class Sleeps:
    """Паузы записываются, а не выдерживаются: тест не должен спать."""

    def __init__(self) -> None:
        self.pauses: list[float] = []

    def __call__(self, seconds: float) -> Awaitable[None]:
        self.pauses.append(seconds)
        return asyncio.sleep(0)


def fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def fixture_chats() -> list[ChatRef]:
    return [ChatRef(**chat) for chat in fixture()["chats"]]


def fixture_replies() -> dict[object, list[FakeMessage]]:
    """Ключ — то, чем адаптер адресует чат: username, а при его отсутствии id."""
    by_id = {chat.tg_id: chat for chat in fixture_chats()}
    replies: dict[object, list[FakeMessage]] = {}
    for raw_id, messages in fixture()["messages"].items():
        chat = by_id[int(raw_id)]
        replies[chat.username or chat.tg_id] = [
            FakeMessage(
                id=message["id"],
                message=message["message"],
                date=None if message["date"] is None else datetime.fromisoformat(message["date"]),
                media=message["media"],
            )
            for message in messages
        ]
    return replies


def adapter(
    client: FakeTelegram,
    chats: Sequence[ChatRef] | None = None,
    *,
    budget_s: float = 40.0,
    sleep: Sleeps | None = None,
) -> TelegramGroupsSource:
    return TelegramGroupsSource(
        directory=FakeDirectory(fixture_chats() if chats is None else chats),
        client=client,
        budget_s=budget_s,
        sleep=sleep or Sleeps(),
    )


async def test_search_maps_messages_to_items() -> None:
    """Обычная выдача: два чата, четыре текстовых поста, один пропущен."""
    client = FakeTelegram(replies=fixture_replies())
    items = await adapter(client).search("байк", {"city": "nha_trang"})

    assert [item.external_id for item in items] == [
        "-1001657234891:55120",
        "-1001657234891:55148",
        "-1001902334455:4471",
    ]
    first = items[0]
    assert first.source == SOURCE_NAME
    assert first.url == "https://t.me/nhatrang_baraholka/55120"
    assert first.posted_at == datetime(2026, 8, 29, 4, 11, 7, tzinfo=UTC)
    assert "Honda Lead 2019" in first.text
    assert first.raw["chat_title"] == "Нячанг · Барахолка"
    assert first.raw["has_media"] is True


async def test_fields_absent_in_telegram_stay_empty() -> None:
    """Автора поста в группе API не отдаёт (spec-v2, 7) — не выдумываем его."""
    items = await adapter(FakeTelegram(replies=fixture_replies())).search("байк", {})
    assert [item.seller_name for item in items] == ["", "", ""]
    assert [item.title for item in items] == ["", "", ""]
    assert [item.price_raw for item in items] == ["", "", ""]
    assert all(item.price_vnd is None for item in items)
    assert all(item.images == [] for item in items)


async def test_empty_result_is_not_a_breakdown() -> None:
    """Ноль объявлений — обычный ответ рынка, а не повод выбывать из плана."""
    client = FakeTelegram(replies={})
    source = adapter(client)
    assert await source.search("вертолёт", {}) == []
    assert source.degraded is False
    assert client.calls.count("get_messages") == 2


async def test_message_without_date_still_reaches_client() -> None:
    """Лот без даты не выбрасывается: живость пометит его `unknown` (3.3)."""
    items = await adapter(FakeTelegram(replies=fixture_replies())).search("байк", {})
    undated = [item for item in items if item.external_id.endswith(":55148")]
    assert len(undated) == 1
    assert undated[0].posted_at is None


async def test_message_without_text_is_skipped() -> None:
    """Фото без подписи объявлением не является."""
    items = await adapter(FakeTelegram(replies=fixture_replies())).search("байк", {})
    skipped = {"-1001657234891:55131", "-1001902334455:4498"}
    assert skipped.isdisjoint({item.external_id for item in items})


async def test_flood_wait_within_budget_is_waited_out() -> None:
    """Telegram попросил подождать — ждём и повторяем, источник живой."""
    client = FakeTelegram(replies=fixture_replies(), floods=[7])
    sleeps = Sleeps()
    source = adapter(client, sleep=sleeps)
    items = await source.search("байк", {})

    assert sleeps.pauses == [7.0]
    assert source.degraded is False
    assert len(items) == 3


async def test_flood_pause_grows_with_each_hit() -> None:
    """Второй FloodWait подряд ждёт дольше первого — пауза экспоненциальная."""
    client = FakeTelegram(replies=fixture_replies(), floods=[5, 0, 5])
    sleeps = Sleeps()
    await adapter(client, sleep=sleeps).search("байк", {})
    assert sleeps.pauses == [5.0, 10.0]


async def test_flood_wait_over_budget_degrades_source() -> None:
    """Ждать дольше остатка бюджета нельзя: источник выбывает, план доигрывает."""
    client = FakeTelegram(replies=fixture_replies(), floods=[300])
    sleeps = Sleeps()
    source = adapter(client, budget_s=5.0, sleep=sleeps)
    items = await source.search("байк", {})

    assert source.degraded is True
    assert sleeps.pauses == []
    assert items == []
    # Второй чат не тронут: с флудом на аккаунте следующий запрос его усугубит.
    assert client.calls.count("get_messages") == 1


async def test_chat_that_floods_twice_is_left_alone() -> None:
    """Две попытки на чат — потолок. Ретрая в цикле нет."""
    client = FakeTelegram(replies=fixture_replies(), floods=[3, 3, 0])
    source = adapter(client, sleep=Sleeps())
    items = await source.search("байк", {})

    assert client.queried == ["nhatrang_baraholka", "nhatrang_baraholka", -1001902334455]
    assert source.degraded is False
    assert [item.external_id for item in items] == ["-1001902334455:4471"]


async def test_no_more_than_ten_chats_per_search() -> None:
    """spec-v2, 2.3: одиннадцатый чат — это FloodWait, а не полнота выдачи."""
    many = [
        ChatRef(tg_id=-1000000000000 - n, username=f"chat{n}", search_rank=n) for n in range(25)
    ]
    client = FakeTelegram(replies={})
    directory = FakeDirectory(many)
    source = TelegramGroupsSource(directory=directory, client=client, sleep=Sleeps())
    await source.search("байк", {})

    assert len(client.queried) == MAX_CHATS_PER_SEARCH
    # Обходим самые приоритетные, а не первые попавшиеся.
    assert client.queried == [f"chat{n}" for n in range(MAX_CHATS_PER_SEARCH)]
    assert directory.asked == [("nha_trang", MAX_CHATS_PER_SEARCH)]


async def test_chats_are_queried_one_after_another() -> None:
    """Параллелить обращения к одному хосту значит выглядеть как атака."""
    client = FakeTelegram(replies=fixture_replies())
    await adapter(client).search("байк", {})
    assert client.events == [
        "start:nhatrang_baraholka",
        "end:nhatrang_baraholka",
        "start:-1001902334455",
        "end:-1001902334455",
    ]


async def test_only_read_methods_are_called() -> None:
    """Юзербот молчит: наружу уходит только чтение."""
    client = FakeTelegram(replies=fixture_replies())
    source = adapter(client)
    await source.search("байк", {})
    await source.aclose()

    assert set(client.calls) <= {"connect", "disconnect", "get_messages"}
    assert "get_messages" in client.calls
    with pytest.raises(AssertionError):
        client.send_message  # noqa: B018


def test_adapter_never_calls_an_outgoing_method() -> None:
    """Страховка от будущей правки: запрет держится кодом, а не памятью."""
    for module in (telegram_groups, telegram_reference, telegram_client):
        path = Path(str(module.__file__))
        assert not OUTGOING_CALL.search(path.read_text(encoding="utf-8")), path.name


async def test_injected_client_is_not_disconnected() -> None:
    """Клиент, который дали снаружи, закрывает тот, кто его создал."""
    client = FakeTelegram(replies={})
    source = adapter(client)
    await source.search("байк", {})
    await source.aclose()
    assert "disconnect" not in client.calls


def test_public_chat_link_opens_for_everyone() -> None:
    chat = ChatRef(tg_id=-1001657234891, username="nhatrang_baraholka")
    assert message_link(chat, 55120) == "https://t.me/nhatrang_baraholka/55120"


def test_private_chat_link_drops_the_service_prefix() -> None:
    """`-100` перед id — служебная разметка Telegram, в ссылке её нет."""
    chat = ChatRef(tg_id=-1001902334455)
    assert message_link(chat, 4471) == "https://t.me/c/1902334455/4471"


@pytest.mark.parametrize(
    ("tg_id", "expected"),
    [
        (-1001902334455, 1902334455),
        # Уже внутренняя форма: у положительного id префикс не снимаем, даже
        # если он сам начинается со 100 — иначе ссылка тихо уедет на чужой чат.
        (1001902334455, 1001902334455),
        (100, 100),
    ],
)
def test_internal_chat_id(tg_id: int, expected: int) -> None:
    assert internal_chat_id(tg_id) == expected


async def test_empty_query_never_reaches_telegram() -> None:
    """Поиск с пустым запросом вернул бы всю группу подряд мимо воронки."""
    client = FakeTelegram(replies=fixture_replies())
    source = adapter(client)
    assert await source.search("   ", {}) == []
    assert client.calls == []
    assert source.degraded is False


async def test_broken_directory_degrades_source() -> None:
    """Реестр чатов недоступен — искать негде, но падать наружу нельзя."""
    source = TelegramGroupsSource(directory=BrokenDirectory(), client=FakeTelegram())
    assert await source.search("байк", {}) == []
    assert source.degraded is True


async def test_one_broken_chat_does_not_cost_the_others() -> None:
    client = FakeTelegram(replies=fixture_replies(), fails={"nhatrang_baraholka"})
    source = adapter(client)
    items = await source.search("байк", {})

    assert [item.external_id for item in items] == ["-1001902334455:4471"]
    assert source.degraded is False


async def test_every_chat_broken_means_broken_source() -> None:
    replies = fixture_replies()
    client = FakeTelegram(replies=replies, fails=set(replies))
    source = adapter(client)
    assert await source.search("байк", {}) == []
    assert source.degraded is True


async def test_messages_limit_is_capped() -> None:
    """Больше сотни Telethon разбивает на несколько RPC — это лишний флуд."""
    client = FakeTelegram(replies={})
    await adapter(client).search("байк", {"limit": 500})
    assert set(client.limits) == {100}


async def test_missing_settings_degrade_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Без строки сессии искать нечем — говорим об этом, а не падаем."""
    blank = Settings(tg_api_id=0, tg_api_hash="", tg_session="")
    monkeypatch.setattr(telegram_groups, "get_settings", lambda: blank)
    source = TelegramGroupsSource(directory=FakeDirectory(fixture_chats()))
    assert await source.search("байк", {}) == []
    assert source.degraded is True


def test_registry_knows_the_source() -> None:
    assert SOURCE_NAME in registered_sources()
    assert isinstance(get_source(SOURCE_NAME), TelegramGroupsSource)


async def test_source_without_chat_registry_stays_quiet() -> None:
    """Пока слоя `db` нет, источник просто ничего не находит."""
    source = get_source(SOURCE_NAME)
    assert await source.search("байк", {}) == []
    assert source.degraded is False
