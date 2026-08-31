"""Адаптер telegram_groups на зафиксированной выдаче messages.search.

Сети здесь нет и быть не должно: Telethon подменён фейком, который знает ровно
разрешённые методы и падает на любом другом обращении. Тест, который ходит в
Telegram, проверяет не адаптер, а связь — и заодно тратит лимиты аккаунта,
ради сохранности которого весь этот адаптер и написан так осторожно.

Фикстура `fixtures/telegram_group_messages.json` — сообщения в том объёме,
который читает адаптер, с крайними случаями: пост без текста, пост без даты,
альбом из двух фото, сообщение в теме форума, чат без username.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from telethon.errors import AuthKeyUnregisteredError, FloodWaitError, SessionRevokedError

from sniffer.config import Settings
from sniffer.sources import telegram_groups
from sniffer.sources.base import get_source, registered_sources
from sniffer.sources.chat_directory import EmptyChatDirectory
from sniffer.sources.telegram_groups import TelegramGroupsSource
from sniffer.sources.telegram_reference import (
    MAX_CHATS_PER_SEARCH,
    SOURCE_NAME,
    ChatRef,
    internal_chat_id,
    message_link,
)

FIXTURE = Path(__file__).parent / "fixtures" / "telegram_group_messages.json"

# Методы, которых у юзербота быть не должно. Вступление в группу и беззвучный
# режим разрешены владельцем (CLAUDE.md), но не этому источнику: он читает.
# Ищем именно вызов, а не упоминание — запрещать имена в документации глупо.
OUTGOING_CALL = re.compile(
    r"\.(send_\w+|forward_\w+|delete_\w+|edit_\w+|join_\w+|mark_read|pin_\w+|"
    r"iter_dialogs|get_dialogs)\s*\("
)


@dataclass(frozen=True, slots=True)
class FakeReplyHeader:
    forum_topic: bool
    reply_to_msg_id: int | None
    reply_to_top_id: int | None


@dataclass(frozen=True, slots=True)
class FakeMessage:
    """Сообщение Telethon в объёме протокола `MessageLike`."""

    id: int
    message: str | None
    date: datetime | None
    media: object | None = None
    grouped_id: int | None = None
    reply_to: FakeReplyHeader | None = None


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
    raises: BaseException | None = None
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
        if self.raises is not None:
            raise self.raises
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

    async def list_active(self, *, city: str, limit: int) -> Sequence[ChatRef]:
        self.asked.append((city, limit))
        return self.chats


class BrokenDirectory:
    async def list_active(self, *, city: str, limit: int) -> Sequence[ChatRef]:
        raise RuntimeError("нет соединения с базой")


class Sleeps:
    """Паузы записываются, а не выдерживаются: тест не должен спать."""

    def __init__(self) -> None:
        self.pauses: list[float] = []

    def __call__(self, seconds: float) -> Awaitable[None]:
        self.pauses.append(seconds)
        return asyncio.sleep(0)


class FakeClock:
    """Часы, которые двигает тест. Иначе бюджет не проверить без ожидания."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def fixture_chats() -> list[ChatRef]:
    return [ChatRef(**chat) for chat in fixture()["chats"]]


def _message(raw: dict[str, Any]) -> FakeMessage:
    reply = raw["reply_to"]
    return FakeMessage(
        id=raw["id"],
        message=raw["message"],
        date=None if raw["date"] is None else datetime.fromisoformat(raw["date"]),
        media=raw["media"],
        grouped_id=raw["grouped_id"],
        reply_to=None if reply is None else FakeReplyHeader(**reply),
    )


def fixture_replies() -> dict[object, list[FakeMessage]]:
    """Ключ — то, чем адаптер адресует чат: username, а при его отсутствии id."""
    by_id = {chat.tg_id: chat for chat in fixture_chats()}
    return {
        by_id[int(raw_id)].username or by_id[int(raw_id)].tg_id: [_message(raw) for raw in messages]
        for raw_id, messages in fixture()["messages"].items()
    }


def adapter(
    client: FakeTelegram,
    chats: Sequence[ChatRef] | None = None,
    *,
    budget_s: float = 40.0,
    sleep: Sleeps | None = None,
    clock: FakeClock | None = None,
) -> TelegramGroupsSource:
    return TelegramGroupsSource(
        directory=FakeDirectory(fixture_chats() if chats is None else chats),
        client=client,
        budget_s=budget_s,
        sleep=sleep or Sleeps(),
        clock=clock or FakeClock(),
    )


async def test_search_maps_messages_to_items() -> None:
    """Обычная выдача: два чата, тексты, ссылки, даты."""
    client = FakeTelegram(replies=fixture_replies())
    items = await adapter(client).search("байк", {"city": "nha_trang"})

    assert [item.external_id for item in items] == [
        "-1001657234891:55120",
        "-1001657234891:55148",
        "-1001657234891:55160",
        "-1001902334455:4471",
        "-1001902334455:4482",
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
    assert [item.seller_name for item in items] == [""] * 5
    assert [item.title for item in items] == [""] * 5
    assert [item.price_raw for item in items] == [""] * 5
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


async def test_album_becomes_one_item_not_five() -> None:
    """Пять фото с одной подписью — одно объявление, а не пять карточек."""
    items = await adapter(FakeTelegram(replies=fixture_replies())).search("байк", {})
    album = [item for item in items if item.raw["grouped_id"] == 13377766655]
    assert [item.external_id for item in album] == ["-1001657234891:55160"]


async def test_album_is_not_repeated_by_the_next_task_of_the_plan() -> None:
    """План ставит до пяти задач на источник — альбом не должен всплыть дважды."""
    source = adapter(FakeTelegram(replies=fixture_replies()))
    first = await source.search("байк", {})
    second = await source.search("скутер", {})
    assert any(item.raw["grouped_id"] == 13377766655 for item in first)
    assert all(item.raw["grouped_id"] != 13377766655 for item in second)


async def test_link_into_a_forum_topic_has_three_segments() -> None:
    """В форумной супергруппе ссылка без номера темы ведёт в никуда.

    Здесь редкая форма: ответ на чужое сообщение внутри темы, и тогда тема
    лежит в `reply_to_top_id`, а `reply_to_msg_id` указывает на то сообщение.
    """
    items = await adapter(FakeTelegram(replies=fixture_replies())).search("байк", {})
    forum = next(item for item in items if item.external_id == "-1001902334455:4471")
    assert forum.url == "https://t.me/c/1902334455/4400/4471"
    assert forum.raw["topic_id"] == 4400


async def test_topic_root_reply_is_the_common_forum_form() -> None:
    """Частая форма: `top_id` пуст, тема — в `reply_to_msg_id`.

    Живая проверка на форумной группе показала, что так приходит бо́льшая часть
    сообщений: пост в теме отвечает прямо в её корень. Раньше фикстура знала
    только редкий вариант с непустым `top_id`, то есть тестами был закрыт как
    раз тот случай, которого в выдаче почти не бывает.
    """
    items = await adapter(FakeTelegram(replies=fixture_replies())).search("байк", {})
    post = next(item for item in items if item.external_id == "-1001902334455:4482")
    assert post.url == "https://t.me/c/1902334455/4400/4482"
    assert post.raw["topic_id"] == 4400


async def test_plain_reply_is_not_a_topic() -> None:
    """Ответ на сообщение в обычной группе номера темы не добавляет."""
    items = await adapter(FakeTelegram(replies=fixture_replies())).search("байк", {})
    reply = next(item for item in items if item.external_id == "-1001657234891:55148")
    assert reply.url == "https://t.me/nhatrang_baraholka/55148"
    assert reply.raw["topic_id"] is None


async def test_flood_wait_within_budget_is_waited_out() -> None:
    """Telegram попросил подождать — ждём и повторяем, источник живой."""
    client = FakeTelegram(replies=fixture_replies(), floods=[7])
    sleeps = Sleeps()
    source = adapter(client, sleep=sleeps)
    items = await source.search("байк", {})

    assert sleeps.pauses == [7.0]
    assert source.degraded is False
    assert len(items) == 5


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


async def test_budget_is_shared_by_all_tasks_of_the_source() -> None:
    """40 с — доля источника в плане, а не подарок каждой его задаче.

    Иначе две задачи `telegram_groups` съедали бы 80 с из 90 с плана и
    остальные источники не успевали бы вовсе.
    """
    clock = FakeClock()
    client = FakeTelegram(replies=fixture_replies(), floods=[])
    sleeps = Sleeps()
    source = adapter(client, budget_s=40.0, sleep=sleeps, clock=clock)

    await source.search("байк", {})
    clock.advance(35.0)
    # На вторую задачу осталось 5 с, и пауза в 20 с в них не помещается.
    client.floods.append(20)
    await source.search("скутер", {})

    assert sleeps.pauses == []
    assert source.degraded is True


async def test_exhausted_budget_stops_the_walk() -> None:
    """Кончились свои 40 с — источник больше не ходит в Telegram.

    Отсчёт начинается с первой задачи источника, а не с создания адаптера:
    план мог простоять в очереди за другими источниками.
    """
    clock = FakeClock()
    client = FakeTelegram(replies=fixture_replies())
    source = adapter(client, budget_s=10.0, clock=clock)

    assert await source.search("байк", {}) != []
    clock.advance(11.0)
    queried_before = client.calls.count("get_messages")

    assert await source.search("скутер", {}) == []
    assert client.calls.count("get_messages") == queried_before
    # Бюджет — не поломка: источник остаётся в плане.
    assert source.degraded is False


async def test_chat_that_floods_twice_is_left_alone() -> None:
    """Две попытки на чат — потолок. Ретрая в цикле нет."""
    client = FakeTelegram(replies=fixture_replies(), floods=[3, 3, 0])
    sleeps = Sleeps()
    source = adapter(client, sleep=sleeps)
    items = await source.search("байк", {})

    assert client.queried == ["nhatrang_baraholka", "nhatrang_baraholka", -1001902334455]
    assert source.degraded is False
    assert [item.external_id for item in items] == [
        "-1001902334455:4471",
        "-1001902334455:4482",
    ]
    # Пауза только перед второй попыткой. После неё попыток нет, чат всё равно
    # бросаем — вторая пауза была бы выброшенным бюджетом остальных чатов.
    assert sleeps.pauses == [3.0]


async def test_last_attempt_flood_over_budget_still_degrades() -> None:
    """Не ждём — но вывод «просят больше, чем осталось» всё равно делаем.

    Пауза после второго флуда не отсыпается, однако её размер продолжает
    говорить о состоянии аккаунта: если Telegram просит больше остатка бюджета,
    следующий чат этот флуд только усугубит, и обход прекращается.
    """
    client = FakeTelegram(replies=fixture_replies(), floods=[3, 30])
    sleeps = Sleeps()
    source = adapter(client, budget_s=10.0, sleep=sleeps)
    await source.search("байк", {})

    assert sleeps.pauses == [3.0], "вторая пауза не отсыпается"
    assert source.degraded is True
    # Второй чат не тронут: с флудом на аккаунте следующий запрос его усугубит.
    assert client.calls.count("get_messages") == 2


async def test_second_flood_on_the_same_chat_still_raises_the_next_pause() -> None:
    """Невыжданный флуд из счётчика не исчезает: лимит у Telegram на аккаунт.

    Первый чат флудит дважды и бросается без второй паузы. Флудов при этом уже
    два, поэтому следующий чат ждёт вчетверо дольше названного минимума, а не
    столько же, сколько первый.
    """
    client = FakeTelegram(replies=fixture_replies(), floods=[5, 5, 5, 0])
    sleeps = Sleeps()
    source = adapter(client, sleep=sleeps)
    await source.search("байк", {})

    assert sleeps.pauses == [5.0, 20.0]


@pytest.mark.parametrize("dead", [AuthKeyUnregisteredError, SessionRevokedError])
async def test_dead_session_stops_on_the_first_chat(dead: Callable[..., BaseException]) -> None:
    """Сессию чинит владелец, а не девять одинаковых отказов подряд."""
    many = [
        ChatRef(tg_id=-1000000000000 - n, username=f"chat{n}", search_rank=n) for n in range(10)
    ]
    client = FakeTelegram(raises=dead(request=None))
    source = adapter(client, many)

    assert await source.search("байк", {}) == []
    assert source.degraded is True
    assert client.calls.count("get_messages") == 1


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


async def test_positive_chat_id_is_refused_loudly() -> None:
    """Положительный id — это ПОЛЬЗОВАТЕЛЬ. Молча искать не там нельзя."""
    broken = ChatRef(tg_id=1657234891, title="битая строка реестра", search_rank=1)
    good = ChatRef(tg_id=-1001902334455, username="ok_chat", search_rank=2)
    client = FakeTelegram(replies={"ok_chat": []})
    source = adapter(client, [broken, good])

    await source.search("байк", {})
    assert client.queried == ["ok_chat"]


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
    """Юзербот молчит: наружу уходит только чтение.

    Вступление в группу и беззвучный режим владелец разрешил (CLAUDE.md), но
    не этому источнику — он ходит по чатам, в которых бот уже состоит.
    """
    client = FakeTelegram(replies=fixture_replies())
    source = adapter(client)
    await source.search("байк", {})
    await source.aclose()

    assert set(client.calls) <= {"connect", "disconnect", "get_messages"}
    assert "get_messages" in client.calls
    with pytest.raises(AssertionError):
        client.send_message  # noqa: B018


def read_path_modules() -> list[Path]:
    """Файлы, из которых собран путь чтения, — по графу импортов.

    Список выведен механически, а не переписан руками, и повод конкретный: у
    голого Telethon для mypy есть любой метод (библиотека без `py.typed`), то
    есть отправку через него не поймают ни mypy, ни ruff. Единственная защита
    от такой правки — этот тест, а перечисленный руками список модулей
    отставал бы от кода: новый файл на пути чтения в него просто не попадал бы.
    """
    root = Path(str(telegram_groups.__file__)).parent.parent
    seen: dict[str, Path] = {}
    queue = [telegram_groups.__name__]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        path = root.parent / (name.replace(".", "/") + ".py")
        if not path.is_file():
            path = root.parent / name.replace(".", "/") / "__init__.py"
            if not path.is_file():
                continue
        seen[name] = path
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                queue.append(node.module)
            elif isinstance(node, ast.Import):
                queue.extend(alias.name for alias in node.names)
    return [path for name, path in seen.items() if name.startswith("sniffer.")]


def test_adapter_never_calls_an_outgoing_method() -> None:
    """Страховка от будущей правки: запрет держится кодом, а не памятью."""
    modules = read_path_modules()
    # Четыре модуля telegram_* плюс то, что они тянут: если граф внезапно
    # схлопнулся до одного файла, тест проверяет не то, что должен.
    assert len(modules) >= 4, [path.name for path in modules]
    for path in modules:
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
    assert message_link(chat, 55120, 900) == "https://t.me/nhatrang_baraholka/900/55120"


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

    assert [item.external_id for item in items] == [
        "-1001902334455:4471",
        "-1001902334455:4482",
    ]
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


async def test_explicitly_disabled_registry_stays_quiet() -> None:
    """Реестр отключён намеренно — источник молчит и остаётся в плане.

    Заглушка теперь подставляется руками: по умолчанию адаптер получает боевой
    реестр из базы, и проверяет это `test_source_wiring.py`. Ровно потому, что
    раньше значением по умолчанию была заглушка, боевой поиск находил ноль.
    """
    source = TelegramGroupsSource(directory=EmptyChatDirectory(), client=FakeTelegram())
    assert await source.search("байк", {}) == []
    assert source.degraded is False


async def test_broken_session_string_degrades_instead_of_throwing() -> None:
    """`TG_SESSION` заполнен, но не валидная StringSession — обрезали в .env.

    Контракт базового класса: наружу не бросаем. И, что важнее, источник обязан
    пометить себя `degraded` — иначе он останется в следующих планах и упадёт
    там точно так же.
    """

    def broken_factory(_settings: object) -> object:
        raise ValueError("Not a valid string")

    source = TelegramGroupsSource(
        directory=FakeDirectory(fixture_chats()),
        reader_factory=broken_factory,  # type: ignore[arg-type]
    )
    items = await source.search("продам", {"city": "nha_trang"})

    assert items == []
    assert source.degraded is True
