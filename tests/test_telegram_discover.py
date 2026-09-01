"""Разведка чатов на зафиксированных сообщениях, без сети и без Telethon.

Сети здесь нет и быть не должно: клиент подменён фейком, который знает ровно
разрешённые методы и падает на любом другом обращении. Тест, который ходит в
Telegram, проверяет не разведку, а связь, — и тратит те самые десять вступлений в
сутки, ради сохранности которых всё и написано так осторожно.

Хранилище тоже фейковое, но устроено как база: `FakeDb` — это «диск», а
репозитории поверх него создаются заново. Перезапуск процесса моделируется
буквально — новые объекты над тем же `FakeDb`, — потому что именно на
перезапуске память и обнуляется, а лимит Telegram нет.
"""

from __future__ import annotations

import ast
import json
import re
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from telethon.errors import (
    ChannelPrivateError,
    ChannelsTooMuchError,
    FloodWaitError,
    InviteRequestSentError,
    UserAlreadyParticipantError,
    UsernameNotOccupiedError,
)

from sniffer.sources import (
    telegram_discover,
    telegram_discover_client,
    telegram_discover_convert,
    telegram_discover_joiner,
    telegram_discover_links,
    telegram_discover_reference,
    telegram_discover_screen,
)
from sniffer.sources.telegram_discover import ChatDiscovery
from sniffer.sources.telegram_discover_joiner import MAX_CANDIDATE_ATTEMPTS, ChatJoiner
from sniffer.sources.telegram_discover_links import candidates_from
from sniffer.sources.telegram_discover_reference import (
    MAX_JOINS_PER_DAY,
    REJECT_ALREADY_INSIDE,
    REJECT_ALREADY_MEMBER,
    REJECT_BOT,
    REJECT_CHANNEL,
    REJECT_CITY_UNKNOWN,
    REJECT_FOREIGN_CITY,
    REJECT_JOIN_REQUEST_SENT,
    REJECT_REQUEST_NEEDED,
    REJECT_TOO_MANY_ATTEMPTS,
    REJECT_UNRESOLVED,
    REJECT_USER,
    ChatCandidate,
    DiscoveredChat,
    JoinState,
    ResolvedChat,
    TelegramJoiner,
)
from sniffer.sources.telegram_discover_screen import screen

FIXTURE = Path(__file__).parent / "fixtures" / "telegram_discover_messages.json"
SEED_SQL = Path(__file__).parents[1] / "infra" / "sql" / "002_seed_candidates.sql"
CHATS_DOC = Path(__file__).parents[1] / "docs" / "chats-nha-trang.md"

CITY = "nha_trang"
NOON = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)

# Методы, которых у юзербота быть не должно (CLAUDE.md, spec-v2 6.1). Из всего
# исходящего разрешены ровно два действия — вступление и беззвучный режим, — и
# они проверяются отдельно, по именам Telethon-запросов.
FORBIDDEN_CALL = re.compile(
    r"\.(send_\w+|forward_\w+|delete_\w+|edit_\w+|mark_read|read_history|"
    r"pin_\w+|unpin_\w+|iter_dialogs|get_dialogs|send_reaction|action)\s*\("
)

# Единственные запросы Telegram, которые вправе встречаться в этих модулях:
# четыре чтения и три запроса на два действия. История идёт через
# высокоуровневый `get_messages`, поэтому в перечень явных TL Request не входит.
# Закрытый список CLAUDE.md
# ограничивает именно
# ДЕЙСТВИЯ — их по-прежнему два, вступление и беззвучный режим. `CheckChatInvite`
# в него не входит и входить не может: это чтение, оно никому не адресовано и в
# чате не видно, ровно как `ResolveUsername` — только для закрытой группы.
ALLOWED_REQUESTS = {
    "ResolveUsernameRequest",
    "GetFullChannelRequest",
    "CheckChatInviteRequest",
    "SearchRequest",
    "JoinChannelRequest",
    "ImportChatInviteRequest",
    "UpdateNotifySettingsRequest",
}

# Из них — ровно два действия. Проверяется отдельно от общего списка: вырасти
# вправе только чтение, а этот набор обязан остаться прежним.
ALLOWED_ACTIONS = {
    "JoinChannelRequest",
    "ImportChatInviteRequest",
    "UpdateNotifySettingsRequest",
}

MODULES = (
    telegram_discover,
    telegram_discover_client,
    telegram_discover_convert,
    telegram_discover_joiner,
    telegram_discover_links,
    telegram_discover_reference,
    telegram_discover_screen,
)


# ── фейковое хранилище ──────────────────────────────────────────────────────


@dataclass
class FakeDb:
    """«Диск». Переживает пересоздание репозиториев — как настоящая база."""

    chats: dict[int, DiscoveredChat] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    rejects: dict[str, str] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    next_event_id: int = 1


@dataclass
class FakeRegistry:
    db: FakeDb

    async def has_chat(self, *, tg_id: int | None = None, username: str = "") -> bool:
        if tg_id is not None and tg_id in self.db.chats:
            return True
        if username:
            wanted = username.lower()
            return any(chat.username.lower() == wanted for chat in self.db.chats.values())
        return False

    async def count(self) -> int:
        return len(self.db.chats)

    async def add(self, chat: DiscoveredChat) -> None:
        self.db.chats[chat.tg_id] = chat


@dataclass
class FakeQueue:
    db: FakeDb

    async def push(self, candidate: ChatCandidate) -> None:
        if any(row["key"] == candidate.key for row in self.db.candidates):
            return
        self.db.candidates.append(
            {
                "key": candidate.key,
                "username": candidate.username,
                "invite_hash": candidate.invite_hash,
                "found_in": candidate.found_in,
                "priority": candidate.priority,
                "status": "queued",
                "seq": len(self.db.candidates),
            }
        )

    async def reserve(self) -> ChatCandidate | None:
        """`FOR UPDATE SKIP LOCKED` по строке с наименьшим приоритетом."""
        queued = [row for row in self.db.candidates if row["status"] == "queued"]
        if not queued:
            return None
        row = min(queued, key=lambda item: (item["priority"], item["seq"]))
        row["status"] = "joining"
        return ChatCandidate(
            key=row["key"],
            username=row["username"],
            invite_hash=row["invite_hash"],
            found_in=row["found_in"],
            priority=row["priority"],
        )

    async def release(self, key: str) -> int:
        for row in self.db.candidates:
            if row["key"] == key:
                row["status"] = "queued"
                row["attempts"] = row.get("attempts", 0) + 1
                return int(row["attempts"])
        return 0

    async def drop(self, key: str) -> None:
        self.db.candidates = [row for row in self.db.candidates if row["key"] != key]

    async def is_queued(self, key: str) -> bool:
        return any(row["key"] == key for row in self.db.candidates)


@dataclass
class FakeRejects:
    db: FakeDb

    async def is_rejected(self, key: str) -> bool:
        return key in self.db.rejects

    async def reject(self, key: str, reason: str) -> None:
        self.db.rejects.setdefault(key, reason)


@dataclass
class FakeLedger:
    """Журнал вступлений так, как его обязан вести слой `db`.

    `claim_slot` одной операцией проверяет ворота и занимает слот: разделение
    на «спросить» и «записать» — это гонка, ради которой тест
    `test_two_workers_get_only_one_join` и существует.
    """

    db: FakeDb

    async def state(self, now: datetime) -> JoinState:
        window = [
            event
            for event in self.db.events
            if event["happened_at"] > now - telegram_discover_reference.JOIN_WINDOW
        ]
        nexts = [e["next_allowed_at"] for e in window if e["next_allowed_at"] is not None]
        blocks = [e["blocked_until"] for e in self.db.events if e["blocked_until"] is not None]
        return JoinState(
            joins_in_window=len(window),
            next_allowed_at=max(nexts) if nexts else None,
            blocked_until=max(blocks) if blocks else None,
        )

    async def claim_slot(self, now: datetime, *, next_allowed_at: datetime) -> int | None:
        state = await self.state(now)
        if state.blocked_until is not None and now < state.blocked_until:
            return None
        if state.joins_in_window >= MAX_JOINS_PER_DAY:
            return None
        if state.next_allowed_at is not None and now < state.next_allowed_at:
            return None
        event_id = self.db.next_event_id
        self.db.next_event_id += 1
        self.db.events.append(
            {
                "id": event_id,
                "kind": "claimed",
                "tg_id": None,
                "username": "",
                "happened_at": now,
                "next_allowed_at": next_allowed_at,
                "blocked_until": None,
                "muted": False,
                "mute_error": None,
            }
        )
        return event_id

    async def confirm_join(self, *, event_id: int, tg_id: int, username: str) -> None:
        event = self._event(event_id)
        event.update(kind="joined", tg_id=tg_id, username=username)

    async def release_slot(self, *, event_id: int) -> None:
        self.db.events = [event for event in self.db.events if event["id"] != event_id]

    async def record_flood(self, *, event_id: int, blocked_until: datetime) -> None:
        self._event(event_id).update(kind="flood", blocked_until=blocked_until)

    async def record_mute_failure(self, *, tg_id: int, error: str) -> None:
        for event in self.db.events:
            if event["tg_id"] == tg_id:
                event.update(muted=False, mute_error=error)

    async def mark_muted(self, *, tg_id: int) -> None:
        for event in self.db.events:
            if event["tg_id"] == tg_id:
                event.update(muted=True, mute_error=None)

    async def pending_mutes(self) -> Sequence[int]:
        return [
            event["tg_id"]
            for event in self.db.events
            if event["kind"] == "joined" and not event["muted"]
        ]

    def _event(self, event_id: int) -> dict[str, Any]:
        for event in self.db.events:
            if event["id"] == event_id:
                return event
        raise AssertionError(f"события {event_id} нет — слот занят не был")


# ── фейковый Telegram ───────────────────────────────────────────────────────


@dataclass
class FakeTelegram:
    """Telegram с разрешённой поверхностью и без сети.

    Любое обращение помимо протокола `TelegramJoiner` — падение теста, а не
    тихая заглушка: молчаливый фейк пропустил бы ровно тот дефект, от которого
    зависит жизнь аккаунта.
    """

    known: dict[str, ResolvedChat] = field(default_factory=dict)
    invites: dict[str, ResolvedChat] = field(default_factory=dict)
    search_results: dict[str, list[ResolvedChat]] = field(default_factory=dict)
    join_errors: dict[str, Exception] = field(default_factory=dict)
    mute_errors: set[int] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)
    joined: list[str] = field(default_factory=list)
    muted: list[int] = field(default_factory=list)

    async def connect(self) -> None:
        self.calls.append("connect")

    async def disconnect(self) -> None:
        self.calls.append("disconnect")

    async def resolve_username(self, username: str) -> ResolvedChat | None:
        self.calls.append(f"resolve:{username}")
        return self.known.get(username.lower())

    async def check_invite(self, invite_hash: str) -> ResolvedChat | None:
        self.calls.append(f"check_invite:{invite_hash}")
        return self.invites.get(invite_hash)

    async def search_contacts(self, query: str, limit: int) -> Sequence[ResolvedChat]:
        self.calls.append(f"search:{query}")
        return self.search_results.get(query, [])[:limit]

    async def join_public(self, username: str) -> int:
        self.calls.append(f"join:{username}")
        if (error := self.join_errors.get(username.lower())) is not None:
            raise error
        self.joined.append(username)
        chat = self.known.get(username.lower())
        return chat.tg_id if chat is not None else -1000000000001

    async def join_invite(self, invite_hash: str) -> int:
        self.calls.append(f"join_invite:{invite_hash}")
        if (error := self.join_errors.get(invite_hash)) is not None:
            raise error
        self.joined.append(invite_hash)
        return -1001111111111

    async def set_muted(self, tg_id: int) -> None:
        self.calls.append(f"mute:{tg_id}")
        if tg_id in self.mute_errors:
            raise RuntimeError("настройки уведомлений недоступны")
        self.muted.append(tg_id)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"юзербот дёрнул запрещённый метод Telegram: {name}")


# ── сборка ──────────────────────────────────────────────────────────────────


def group(username: str, title: str, about: str = "", tg_id: int = 0) -> ResolvedChat:
    return ResolvedChat(
        tg_id=tg_id or -(10**12) - zlib.crc32(username.encode()),
        username=username,
        title=title,
        about=about,
        is_group=True,
    )


def invite(
    title: str,
    about: str = "",
    *,
    is_group: bool = True,
    request_needed: bool = False,
    already_member: bool = False,
) -> ResolvedChat:
    """Что отдаёт `checkChatInvite` о чате, в котором мы не состоим.

    `tg_id` нулевой и `username` пустой не для удобства: до вступления Telegram
    ни того, ни другого о закрытом чате не сообщает. Отбор обязан решать по
    названию и описанию.
    """
    return ResolvedChat(
        tg_id=0,
        username="",
        title=title,
        about=about,
        is_group=is_group,
        request_needed=request_needed,
        already_member=already_member,
    )


def discovery(db: FakeDb, client: TelegramJoiner) -> ChatDiscovery:
    """Отбор над «диском»: в Telegram уходит только чтение."""
    return ChatDiscovery(
        registry=FakeRegistry(db),
        queue=FakeQueue(db),
        rejected=FakeRejects(db),
        client=client,
        city=CITY,
    )


def joiner(
    db: FakeDb,
    client: TelegramJoiner,
    *,
    now: datetime = NOON,
    max_tracked: int = 50,
    jitter: timedelta = timedelta(0),
) -> ChatJoiner:
    """Очередь вступлений. Разброс паузы обнулён — проверяем минимум."""
    return ChatJoiner(
        registry=FakeRegistry(db),
        queue=FakeQueue(db),
        ledger=FakeLedger(db),
        rejected=FakeRejects(db),
        client=client,
        city=CITY,
        max_tracked=max_tracked,
        now=lambda: now,
        jitter=lambda: jitter,
    )


@dataclass(frozen=True, slots=True)
class FakeEntity:
    url: str | None


@dataclass(frozen=True, slots=True)
class FakeMessage:
    id: int
    message: str | None
    entities: Sequence[FakeEntity] | None = None


def fixture_messages() -> list[FakeMessage]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [
        FakeMessage(
            id=item["id"],
            message=item["message"],
            entities=(
                None
                if item["entities"] is None
                else [FakeEntity(url=entity.get("url")) for entity in item["entities"]]
            ),
        )
        for item in raw
    ]


def keys_of(message: FakeMessage) -> list[str]:
    return [candidate.key for candidate in candidates_from(message)]


# ── извлечение ссылок ───────────────────────────────────────────────────────


def test_plain_link_gives_a_candidate() -> None:
    assert keys_of(fixture_messages()[0]) == ["@nyachang_arendaa"]


def test_link_to_a_post_still_names_its_chat() -> None:
    """`t.me/<chat>/<msg>` — тоже адрес чата, терять его глупо."""
    assert keys_of(fixture_messages()[1]) == ["@nyachang_uslugi"]


def test_joinchat_invite() -> None:
    assert keys_of(fixture_messages()[2]) == ["+AbCdEfGhIjKlMnOpQr"]


def test_plus_invite() -> None:
    """Новая форма приглашения — та же ссылка, другой синтаксис."""
    assert keys_of(fixture_messages()[3]) == ["+XyZ1234567890abcdef"]


def test_mention_in_text() -> None:
    assert keys_of(fixture_messages()[4]) == ["@nhatrang_pro_house"]


def test_link_hidden_in_a_message_entity() -> None:
    """`MessageEntityTextUrl`: в тексте адреса нет вовсе, только подпись.

    Самая частая форма перекрёстной ссылки. Разбор одного лишь текста
    пропустил бы её целиком, а вместе с ней — половину находок.
    """
    message = fixture_messages()[5]
    assert "t.me" not in (message.message or "")
    assert keys_of(message) == ["@barakholka_nyachang"]


def test_service_paths_and_email_are_not_chats() -> None:
    """`t.me/c/…` — пост в чате без имени, `@` в почте — не упоминание."""
    assert keys_of(fixture_messages()[6]) == []


def test_message_without_text_or_entities() -> None:
    assert keys_of(fixture_messages()[7]) == []


def test_invite_hash_keeps_its_case() -> None:
    """`+AbC` и `+abc` — разные приглашения, username — одно и то же имя."""
    upper = candidates_from(FakeMessage(1, "t.me/+AbCdEfGhIjKlMnOpQr"))
    named = candidates_from(FakeMessage(2, "t.me/NyaChang_Uslugi"))
    assert upper[0].key == "+AbCdEfGhIjKlMnOpQr"
    assert named[0].key == "@nyachang_uslugi"
    assert named[0].username == "NyaChang_Uslugi"


def test_same_chat_twice_in_one_message_is_one_candidate() -> None:
    message = FakeMessage(
        1,
        "t.me/nyachang_uslugi и ещё раз @nyachang_uslugi",
        [FakeEntity(url="https://t.me/nyachang_uslugi")],
    )
    assert keys_of(message) == ["@nyachang_uslugi"]


def test_link_at_the_end_of_a_sentence() -> None:
    """«Заходи на t.me/chat.» — точка не часть имени."""
    assert keys_of(FakeMessage(1, "Заходи на t.me/nyachang_pro.")) == ["@nyachang_pro"]


# ── отсев ───────────────────────────────────────────────────────────────────


def test_channel_is_rejected() -> None:
    """В канале нет объявлений от людей — только вещание владельца."""
    channel = ResolvedChat(tg_id=-1, username="nhatrang_news", title="Нячанг новости")
    assert screen(channel, city=CITY) == REJECT_CHANNEL


def test_bot_is_rejected() -> None:
    bot = ResolvedChat(
        tg_id=7, username="nyachang_bot", title="Нячанг бот", is_bot=True, is_user=True
    )
    assert screen(bot, city=CITY) == REJECT_BOT


def test_user_is_rejected() -> None:
    user = ResolvedChat(tg_id=7, username="ivan_nyachang", title="Иван", is_user=True)
    assert screen(user, city=CITY) == REJECT_USER


def test_foreign_city_is_rejected() -> None:
    assert screen(group("danang001", "Дананг барахолка"), city=CITY) == REJECT_FOREIGN_CITY


def test_chat_without_a_city_is_rejected() -> None:
    """Не «наверное наш»: вступление стоит суток ожидания и места под потолком."""
    assert screen(group("sales_all", "Продам всё"), city=CITY) == REJECT_CITY_UNKNOWN


def test_city_is_found_in_any_transliteration() -> None:
    for username, title in (
        ("nyachang_uslugi", "Нячанг услуги"),
        ("arenda_v_nyachang", "Аренда"),
        ("nhatrangapartment", "Apartments"),
        ("chat", "Nha Trang chat"),
    ):
        assert screen(group(username, title), city=CITY) == "", username


def test_city_is_found_in_the_description() -> None:
    """Название бывает бесполезным — описание спасает: оба даёт `resolve`."""
    chat = group("auto_moto_vietnam", "Аренда байков", about="Мотобайки в Нячанге")
    assert screen(chat, city=CITY) == ""


def test_mixed_chat_with_our_city_is_kept() -> None:
    """«Нячанг и Дананг» оставляем: лишнее отсеет гейт, отказ дороже ошибки."""
    assert screen(group("mixed", "Нячанг и Дананг аренда"), city=CITY) == ""


async def test_screening_never_joins() -> None:
    """Решение принимается чтением, без единого вступления — по обеим формам.

    Приглашение проходит те же ворота, что и username: `checkChatInvite` отдаёт
    название, описание и тип, ничего не отправляя. Поблажки у хэша нет — и быть
    не может, потому что выйти из чата закрытый список CLAUDE.md не разрешает.
    """
    db = FakeDb()
    client = FakeTelegram(
        known={
            "nyachang_arendaa": group("nyachang_arendaa", "Нячанг аренда"),
            "nyachang_uslugi": group("nyachang_uslugi", "Нячанг услуги"),
            "nhatrang_pro_house": ResolvedChat(
                tg_id=-3, username="nhatrang_pro_house", title="Nha Trang дом"
            ),
            "barakholka_nyachang": group("barakholka_nyachang", "Барахолка Нячанг"),
        },
        invites={
            "AbCdEfGhIjKlMnOpQr": invite("Барахолка Нячанг закрытая"),
            # Города не видно ни в названии, ни в описании — вступать вслепую
            # нельзя, откатить вступление нечем.
            "XyZ1234567890abcdef": invite("Частный клуб", "только для своих"),
        },
    )
    await discovery(db, client).harvest(fixture_messages(), found_in="@source_chat")

    assert not any(call.startswith("join") for call in client.calls)
    # Канал и приглашение без города отбракованы; остальное — в очередь.
    assert sorted(row["key"] for row in db.candidates) == [
        "+AbCdEfGhIjKlMnOpQr",
        "@barakholka_nyachang",
        "@nyachang_arendaa",
        "@nyachang_uslugi",
    ]
    assert db.rejects["@nhatrang_pro_house"] == REJECT_CHANNEL
    assert db.rejects["+XyZ1234567890abcdef"] == REJECT_CITY_UNKNOWN


async def test_known_and_rejected_candidates_are_not_resolved_again() -> None:
    """Ссылка на дананговскую барахолку встречается в чатах постоянно."""
    db = FakeDb(rejects={"@danang001": REJECT_FOREIGN_CITY})
    db.chats[-99] = DiscoveredChat(-99, "nyachang_uslugi", "Нячанг услуги", CITY)
    client = FakeTelegram()
    added = await discovery(db, client).harvest(
        [FakeMessage(1, "t.me/danang001 и t.me/nyachang_uslugi")]
    )

    assert added == 0
    assert client.calls == []


async def test_vocabulary_search_reuses_the_resolved_chat() -> None:
    """`contacts.Search` уже отдал название — второй `resolve` был бы лишним."""
    db = FakeDb()
    client = FakeTelegram(
        search_results={
            "нячанг барахолка": [
                group("baraholka_nachang", "Барахолка Нячанг"),
                group("danang_baraholka", "Дананг барахолка"),
            ]
        }
    )
    added = await discovery(db, client).harvest_vocabulary(["нячанг барахолка", "   "])

    assert added == 1
    assert client.calls == ["search:нячанг барахолка"]
    assert [row["key"] for row in db.candidates] == ["@baraholka_nachang"]
    assert db.rejects["@danang_baraholka"] == REJECT_FOREIGN_CITY


# ── очередь вступлений ──────────────────────────────────────────────────────


def seeded_db(*usernames: str) -> FakeDb:
    db = FakeDb()
    for index, username in enumerate(usernames):
        db.candidates.append(
            {
                "key": f"@{username}",
                "username": username,
                "invite_hash": "",
                "found_in": "seed",
                "priority": 10 + index,
                "status": "queued",
                "seq": index,
            }
        )
    return db


def seeded_invite_db(*hashes: str) -> FakeDb:
    """Очередь из приглашений. Сид владельца их не содержит, разведка — да."""
    db = FakeDb()
    for index, invite_hash in enumerate(hashes):
        db.candidates.append(
            {
                "key": f"+{invite_hash}",
                "username": "",
                "invite_hash": invite_hash,
                "found_in": "@source_chat",
                "priority": 100 + index,
                "status": "queued",
                "seq": index,
            }
        )
    return db


def joiner_client(*usernames: str) -> FakeTelegram:
    return FakeTelegram(
        known={
            name: group(name, "Нячанг барахолка", tg_id=-(10**12) - index)
            for index, name in enumerate(usernames, start=1)
        }
    )


async def test_queue_is_drained_in_priority_order() -> None:
    """Волна 1 раньше волны 3: очередь живёт неделями, порядок решает выдачу."""
    db = seeded_db("first", "second")
    client = joiner_client("first", "second")
    chat = await joiner(db, client, now=NOON).join_next()
    assert chat is not None and chat.username == "first"


async def test_tenth_join_passes_and_the_eleventh_does_not() -> None:
    """CLAUDE.md: не больше 10 вступлений в сутки. Проверяем на границе."""
    names = tuple(f"chat{number}" for number in range(1, 12))
    db = seeded_db(*names)
    client = joiner_client(*names)
    # Пауза в час выдержана — упирается именно в суточный счётчик.
    moments = [NOON + timedelta(hours=hour) for hour in range(11)]

    joined = [await joiner(db, client, now=moment).join_next() for moment in moments]

    assert [chat.username for chat in joined[:10] if chat is not None] == list(names[:10])
    assert joined[10] is None
    assert client.joined == list(names[:10])
    # Одиннадцатый кандидат остался в очереди, а не потерялся.
    assert any(row["key"] == "@chat11" and row["status"] == "queued" for row in db.candidates)


async def test_the_eleventh_join_is_still_refused_after_a_restart() -> None:
    """Процесс перезапускается, а лимит Telegram нет.

    Счётчик в памяти обнулился бы вместе с процессом и разрешил бы ещё десять
    вступления после каждого деплоя. Здесь «диск» тот же, объекты новые.
    """
    names = tuple(f"chat{number}" for number in range(1, 12))
    db = seeded_db(*names)
    client = joiner_client(*names)
    for hour in range(10):
        await joiner(db, client, now=NOON + timedelta(hours=hour)).join_next()

    # Перезапуск: разведка, реестр, очередь и журнал созданы заново.
    restarted = joiner(db, joiner_client(*names), now=NOON + timedelta(hours=10))
    assert await restarted.join_next() is None
    assert len(db.chats) == MAX_JOINS_PER_DAY

    # А через сутки после первого вступления окно сдвинулось и слот освободился.
    later = joiner(db, client, now=NOON + timedelta(hours=24, minutes=1))
    chat = await later.join_next()
    assert chat is not None and chat.username == "chat11"


async def test_an_hour_must_pass_between_joins() -> None:
    db = seeded_db("one", "two")
    client = joiner_client("one", "two")
    assert await joiner(db, client, now=NOON).join_next() is not None
    assert await joiner(db, client, now=NOON + timedelta(minutes=59)).join_next() is None
    assert await joiner(db, client, now=NOON + timedelta(minutes=61)).join_next() is not None


async def test_pause_is_not_exactly_an_hour_when_jitter_is_real() -> None:
    """«Вразнобой, не по таймеру»: ровно час в ровно час — подпись автомата."""
    db = seeded_db("one", "two")
    client = joiner_client("one", "two")
    await joiner(db, client, now=NOON, jitter=timedelta(minutes=17)).join_next()
    assert db.events[0]["next_allowed_at"] == NOON + timedelta(hours=1, minutes=17)


async def test_flood_stops_joins_until_the_end_of_the_day() -> None:
    """Флуд-лимит на join — предупреждение, а не задержка на указанные секунды."""
    db = seeded_db("one", "two")
    client = joiner_client("one", "two")
    client.join_errors["one"] = FloodWaitError(request=None, capture=30)

    assert await joiner(db, client, now=NOON).join_next() is None
    assert db.events[0]["blocked_until"] == datetime(2026, 9, 1, tzinfo=UTC)

    # Названные Telegram 30 секунд ничего не значат: и через минуту, и к вечеру
    # вступлений нет.
    for shift in (timedelta(minutes=1), timedelta(hours=11, minutes=30)):
        assert await joiner(db, client, now=NOON + shift).join_next() is None
    assert client.joined == []

    # После полуночи очередь снова живая, и кандидат никуда не делся.
    client.join_errors.clear()
    resumed = await joiner(db, client, now=datetime(2026, 9, 1, 0, 1, tzinfo=UTC)).join_next()
    assert resumed is not None and resumed.username == "one"


async def test_two_workers_get_only_one_join() -> None:
    """Проверить и потом действовать — это гонка. Слот занимается атомарно."""
    db = seeded_db("one", "two")
    client = joiner_client("one", "two")
    first = joiner(db, client, now=NOON)
    second = joiner(db, client, now=NOON)

    assert await first.join_next() is not None
    assert await second.join_next() is None
    assert client.joined == ["one"]


async def test_reserved_candidate_is_not_taken_twice() -> None:
    """Пока один воркер вступает, второй не должен брать того же кандидата."""
    db = seeded_db("one")
    queue = FakeQueue(db)
    assert (await queue.reserve()) is not None
    assert (await queue.reserve()) is None


async def test_lost_connection_costs_a_slot_but_not_the_candidate() -> None:
    """Обрыв посреди вступления: прошло оно или нет — неизвестно.

    Слот остаётся потраченным (недосчитать безопаснее, чем сделать одиннадцатое),
    кандидат возвращается в очередь — разбирать её больше некому.
    """
    db = seeded_db("one")
    client = joiner_client("one")
    client.join_errors["one"] = ConnectionError("соединение разорвано")

    assert await joiner(db, client, now=NOON).join_next() is None
    assert [row["status"] for row in db.candidates] == ["queued"]
    assert len(db.events) == 1
    assert db.events[0]["kind"] == "claimed"
    # Слот потрачен: в тот же час второй попытки нет.
    assert await joiner(db, client, now=NOON + timedelta(minutes=5)).join_next() is None


async def test_a_refused_candidate_still_spends_the_slot() -> None:
    """Telegram сказал «такого нет» — вступления не было, но ЗАПРОС состоялся.

    Тест переписан: раньше он требовал обратного — что слот возвращается и
    следующий кандидат идёт сразу, без часа ожидания. Это и оказалось дырой в
    лимите. `release_slot` удаляет событие, а вместе с ним `next_allowed_at`,
    то есть паузу в час; на очереди из недоступных чатов замер дал 24 исходящих
    `JoinChannel` за сутки вместо трёх, а при остановленном времени — 40.
    Флуд-лимит Telegram считает запросы, а не успехи.

    Принцип записан в spec-v2 4.5 прямым текстом, и он сильнее удобства:
    «слот потрачен и тогда, когда чата мы не получили, но запрос наружу
    состоялся».
    """
    db = seeded_db("gone", "alive")
    client = joiner_client("gone", "alive")
    client.join_errors["gone"] = UsernameNotOccupiedError(request=None)

    assert await joiner(db, client, now=NOON).join_next() is None
    assert db.rejects["@gone"] == telegram_discover_reference.REJECT_JOIN_REFUSED
    # Слот потрачен: следующий кандидат ждёт час, как после успешного вступления.
    assert await joiner(db, client, now=NOON).join_next() is None
    later = await joiner(db, client, now=NOON + timedelta(hours=1, minutes=1)).join_next()
    assert later is not None and later.username == "alive"


async def test_chat_cap_stops_growth() -> None:
    """Потолок достигнут — дальше только замена, не рост (CLAUDE.md)."""
    db = seeded_db("one")
    db.chats[-7] = DiscoveredChat(-7, "old", "Старый", CITY)
    client = joiner_client("one")

    assert await joiner(db, client, now=NOON, max_tracked=1).join_next() is None
    assert client.joined == []
    # Слот не потрачен: вступления не было, ждать час не за что.
    assert db.events == []


async def test_empty_queue_does_not_burn_a_slot() -> None:
    db = FakeDb()
    assert await joiner(db, FakeTelegram(), now=NOON).join_next() is None
    assert db.events == []


async def test_joined_chat_lands_in_the_registry_for_the_reader() -> None:
    """Дальше его читает обычный `telegram_groups` — его код не менялся."""
    db = seeded_db("one")
    client = joiner_client("one")
    chat = await joiner(db, client, now=NOON).join_next()

    assert chat is not None
    assert db.chats[chat.tg_id] == chat
    assert chat.city == CITY
    # Ранг хуже курируемых: находка не вытесняет выбранное владельцем.
    assert chat.search_rank == telegram_discover_reference.DISCOVERED_RANK


# ── беззвучный режим ────────────────────────────────────────────────────────


async def test_mute_is_set_right_after_joining() -> None:
    db = seeded_db("one")
    client = joiner_client("one")
    chat = await joiner(db, client, now=NOON).join_next()

    assert chat is not None
    assert client.muted == [chat.tg_id]
    assert client.calls.index(f"join:{chat.username}") < client.calls.index(f"mute:{chat.tg_id}")
    assert db.events[0]["muted"] is True


async def test_failed_mute_does_not_undo_the_join() -> None:
    """Вступление стоило суточного лимита, беззвучность чинится повтором."""
    db = seeded_db("one")
    client = joiner_client("one")
    client.mute_errors.add(next(iter(client.known.values())).tg_id)

    chat = await joiner(db, client, now=NOON).join_next()

    assert chat is not None
    assert chat.tg_id in db.chats
    assert db.events[0]["kind"] == "joined"
    assert db.events[0]["muted"] is False
    assert db.events[0]["mute_error"]


async def test_pending_mute_is_retried_later() -> None:
    db = seeded_db("one")
    client = joiner_client("one")
    tg_id = next(iter(client.known.values())).tg_id
    client.mute_errors.add(tg_id)
    await joiner(db, client, now=NOON).join_next()

    client.mute_errors.clear()
    assert await joiner(db, client, now=NOON).retry_mutes() == 1
    assert client.muted == [tg_id]
    assert db.events[0]["muted"] is True


async def test_mute_never_touches_a_chat_outside_our_registry() -> None:
    """`UpdateNotifySettings` меняет настройки живого человека, не сервиса."""
    db = FakeDb()
    db.events.append(
        {
            "id": 1,
            "kind": "joined",
            "tg_id": -1002222222222,
            "username": "someone_elses",
            "happened_at": NOON,
            "next_allowed_at": None,
            "blocked_until": None,
            "muted": False,
            "mute_error": None,
        }
    )
    client = FakeTelegram()
    assert await joiner(db, client, now=NOON).retry_mutes() == 0
    assert client.muted == []
    assert client.calls == []


# ── граница «только чтение плюс два действия» ───────────────────────────────


async def test_only_allowed_methods_reach_telegram() -> None:
    """Юзербот молчит: наружу уходят чтение, вступление и беззвучный режим."""
    db = seeded_db("one")
    client = joiner_client("one")
    await joiner(db, client, now=NOON).join_next()

    assert {call.split(":")[0] for call in client.calls} <= {
        "connect",
        "disconnect",
        "resolve",
        "search",
        "join",
        "join_invite",
        "mute",
    }
    with pytest.raises(AssertionError):
        client.send_message  # noqa: B018


def discovery_modules() -> list[Path]:
    """Файлы разведки — по ГРАФУ ИМПОРТОВ, а не по имени файла.

    Так же, как на пути чтения (`test_telegram_groups.read_path_modules`), и по
    той же причине. Список по маске `telegram_discover*.py` обходился одним
    переименованием: модуль `sources/discover_helper.py`, импортированный из
    разведки, с прямым `client.send_message(...)` внутри, проходил ВСЕ ворота
    зелёными — mypy и ruff на таком вызове слепы (Telethon без `py.typed`, для
    них это `Any`), а страж его просто не видел. Проверено на копии дерева.
    """
    root = Path(str(telegram_discover.__file__)).parent.parent
    seen: dict[str, Path] = {}
    # Обход начинается от ВСЕХ охраняемых модулей, а не от одной точки входа.
    # Проверяемое свойство — замкнутость: если охраняемый модуль что-то
    # импортирует, это «что-то» тоже под стражем. Так закрывается обход новым
    # файлом с любым именем и в любом каталоге.
    queue = [module.__name__ for module in MODULES]
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


def test_every_discovery_module_is_under_guard() -> None:
    """Список `MODULES` обязан покрывать все модули разведки.

    Без этой проверки страж закрытого списка обходится не правкой запрета, а
    новым файлом: код переезжает в модуль, которого в списке нет, и запросы к
    Telegram перестают проверяться молча.

    Список берётся из графа импортов, поэтому обойти его переименованием
    нельзя: файл, который разведка импортирует, попадает под стража сам —
    как бы он ни назывался и в каком бы каталоге ни лежал.
    """
    listed = {Path(str(module.__file__)).stem for module in MODULES}
    # Разведка тянет и общее (config, db, домен), но клиент Telegram живёт в
    # `sources` — там и должно быть проверено ВСЁ, до чего она дотягивается.
    reachable = {path.stem for path in discovery_modules() if path.parent.name == "sources"}
    # `telegram_groups` исключён: это отдельный источник со своим стражем
    # (`test_telegram_groups`), разведка лишь берёт у него реестр чатов.
    reachable -= {"telegram_groups", "telegram_reference", "telegram_client", "telegram_mapping"}

    assert reachable <= listed, (
        f"под стражем нет модулей, до которых разведка дотягивается: {sorted(reachable - listed)}"
    )


def test_modules_call_no_forbidden_method() -> None:
    """Страховка от будущей правки: запрет держится кодом, а не памятью."""
    for module in MODULES:
        path = Path(str(module.__file__))
        assert not FORBIDDEN_CALL.search(path.read_text(encoding="utf-8")), path.name


def test_only_seven_telegram_requests_exist_in_the_code() -> None:
    """Четыре чтения и три запроса на два действия (CLAUDE.md).

    Именно перечисление, а не запрет по образцу: запретный список пропускает
    то, чего в нём не додумались написать, а этот тест краснеет на любом новом
    запросе к Telegram — включая тот, который автор считал безобидным.
    """
    used: set[str] = set()
    for module in MODULES:
        path = Path(str(module.__file__))
        used |= set(re.findall(r"\b(\w+Request)\b", path.read_text(encoding="utf-8")))
    assert used == ALLOWED_REQUESTS


def test_only_two_outgoing_actions_exist_in_the_code() -> None:
    """Чтение вправе расти, действий по-прежнему ровно два (CLAUDE.md).

    Отдельно от общего списка запросов: закрытый список владельца ограничивает
    именно исходящие действия. Новое чтение — вопрос инженерный, новое действие —
    вопрос к владельцу, и смешивать их в одном тесте значит потерять эту разницу.
    """
    used: set[str] = set()
    for module in MODULES:
        path = Path(str(module.__file__))
        used |= set(re.findall(r"\b(\w+Request)\b", path.read_text(encoding="utf-8")))
    assert used & ALLOWED_ACTIONS == ALLOWED_ACTIONS
    assert not used - ALLOWED_REQUESTS


def test_the_joiner_protocol_has_no_extra_methods() -> None:
    """Дописать отправку молча не выйдет: у типа таких методов нет."""
    methods = {name for name in vars(TelegramJoiner) if not name.startswith("_")}
    assert methods == {
        "connect",
        "disconnect",
        "resolve_username",
        "check_invite",
        "search_contacts",
        "history",
        "join_public",
        "join_invite",
        "set_muted",
    }


def test_limits_match_the_rules_they_were_copied_from() -> None:
    """Числа — из CLAUDE.md, а не из удобства."""
    assert MAX_JOINS_PER_DAY == 10
    assert telegram_discover_reference.MIN_JOIN_PAUSE == timedelta(hours=1)
    assert telegram_discover_reference.JOIN_WINDOW == timedelta(hours=24)


# ── стартовый набор ─────────────────────────────────────────────────────────


def doc_waves() -> list[str]:
    """Порядок вступления так, как он записан в документе."""
    text = CHATS_DOC.read_text(encoding="utf-8")
    body = text[text.index("### Волна 1") : text.index("### Не наш город")]
    return re.findall(r"`@([A-Za-z0-9_]+)`", body)


def seed_rows() -> list[tuple[str, str, int]]:
    sql = SEED_SQL.read_text(encoding="utf-8")
    start = sql.index("INSERT INTO chat_candidates")
    block = sql[start : sql.index("ON CONFLICT", start)]
    found = re.findall(r"\('(@[^']+)', '([^']+)', '[^']*', (\d+)\)", block)
    return [(key, username, int(priority)) for key, username, priority in found]


def test_seed_repeats_the_document_in_the_same_order() -> None:
    """Стартовый набор — тот самый список владельца, а не пересказ по памяти."""
    rows = seed_rows()
    assert len(rows) == 35, "сид разобрался пустым — проверка была бы бессмысленной"
    assert [username for _, username, _ in rows] == doc_waves()
    assert [priority for *_, priority in rows] == sorted(priority for *_, priority in rows), (
        "приоритеты обязаны возрастать, иначе порядок документа теряется"
    )


def test_seed_keys_are_normalised_like_the_code_normalises_them() -> None:
    """Ключ из сида и ключ из живой ссылки обязаны совпадать до буквы.

    Разойдись они регистром — и разведка заведёт второго кандидата на тот же
    чат, а потом потратит на него вступление.
    """
    rows = seed_rows()
    assert rows
    for key, username, _ in rows:
        assert key == candidates_from(FakeMessage(1, f"t.me/{username}"))[0].key


def test_foreign_cities_from_the_document_are_pre_rejected() -> None:
    """Иначе первая же ссылка на @danang001 стоит нам `resolve_username`."""
    sql = SEED_SQL.read_text(encoding="utf-8")
    rejected = set(re.findall(r"\('(@[a-z0-9_]+)', 'foreign_city'\)", sql))
    text = CHATS_DOC.read_text(encoding="utf-8")
    tail = text[text.index("### Не наш город") : text.index("## Что дальше")]
    assert rejected == {f"@{name.lower()}" for name in re.findall(r"`@([A-Za-z0-9_]+)`", tail)}


def test_seed_fits_under_the_chat_cap() -> None:
    """Сид, который сам по себе пробивает потолок, — это лимит, который врёт."""
    assert len(seed_rows()) <= telegram_discover_reference.MAX_TRACKED_CHATS


def test_seed_tables_exist_in_the_schema() -> None:
    """Сид и схема лежат рядом и обязаны сходиться по именам колонок."""
    schema = (Path(__file__).parents[1] / "infra" / "sql" / "001_init.sql").read_text(
        encoding="utf-8"
    )
    for fragment in ("chat_candidates", "chat_rejects", "chat_join_events", "priority"):
        assert fragment in schema, fragment


# ── приглашения: те же ворота, что у username ───────────────────────────────


@pytest.mark.parametrize(
    ("resolved", "reason"),
    [
        (invite("Нячанг новости", is_group=False), REJECT_CHANNEL),
        (invite("Дананг барахолка"), REJECT_FOREIGN_CITY),
        (invite("Частный клуб"), REJECT_CITY_UNKNOWN),
        (invite("Барахолка Нячанг", request_needed=True), REJECT_REQUEST_NEEDED),
        (invite("Барахолка Нячанг", already_member=True), REJECT_ALREADY_MEMBER),
    ],
)
async def test_invite_is_rejected_before_any_join(resolved: ResolvedChat, reason: str) -> None:
    """Владелец просил именно это: отбраковка происходит ДО вступления.

    Раньше кандидат с хэшем ставился в очередь без единой проверки, а «ворота
    вступления», на которые ссылался комментарий, не существовали. Проверять
    после вступления нечем: выйти из чата закрытый список CLAUDE.md не
    разрешает, поэтому ошибка была бы невозвратной.
    """
    db = FakeDb()
    client = FakeTelegram(invites={"HaShAbCdEfGh1234": resolved})
    added = await discovery(db, client).harvest([FakeMessage(1, "t.me/+HaShAbCdEfGh1234")])

    assert added == 0
    assert db.candidates == []
    assert db.rejects["+HaShAbCdEfGh1234"] == reason
    assert not any(call.startswith("join") for call in client.calls)


async def test_unreadable_invite_is_rejected_not_queued() -> None:
    """Telegram не рассказал о приглашении ничего — вступать вслепую нельзя."""
    db = FakeDb()
    client = FakeTelegram(invites={})
    assert await discovery(db, client).harvest([FakeMessage(1, "t.me/+HaShAbCdEfGh1234")]) == 0
    assert db.rejects["+HaShAbCdEfGh1234"] == REJECT_UNRESOLVED


async def test_invite_with_our_city_is_queued_and_then_joined() -> None:
    """Путь приглашения целиком: чтение → очередь → вступление → реестр.

    До этого теста `join_invite` не вызывался ни разу ни одним тестом: в
    очереди были только username-кандидаты, и вся ветка вступления по хэшу
    существовала непроверенной.
    """
    db = FakeDb()
    client = FakeTelegram(
        invites={"GoOdHaShAbCdEf12": invite("Барахолка Нячанг", "аренда и продажа")}
    )
    added = await discovery(db, client).harvest([FakeMessage(1, "t.me/+GoOdHaShAbCdEf12")])
    assert added == 1
    assert [row["key"] for row in db.candidates] == ["+GoOdHaShAbCdEf12"]

    chat = await joiner(db, client, now=NOON).join_next()

    assert chat is not None
    assert client.joined == ["GoOdHaShAbCdEf12"]
    assert "join_invite:GoOdHaShAbCdEf12" in client.calls
    # Город в записи реестра — не «по умолчанию», а тот, который отбор нашёл в
    # названии приглашения. Именно за него потом фильтрует читающий адаптер.
    assert chat.city == CITY
    assert db.chats[chat.tg_id].city == CITY
    assert db.candidates == []


async def test_invite_hash_keeps_its_case_through_the_whole_path() -> None:
    """`+AbC` и `+abc` — разные ссылки: хэш регистрозависим."""
    db = FakeDb()
    client = FakeTelegram(invites={"MiXeDCaSeAbCdEf1": invite("Нячанг барахолка")})
    await discovery(db, client).harvest([FakeMessage(1, "t.me/+MiXeDCaSeAbCdEf1")])
    assert [row["key"] for row in db.candidates] == ["+MiXeDCaSeAbCdEf1"]
    await joiner(db, client, now=NOON).join_next()
    assert client.joined == ["MiXeDCaSeAbCdEf1"]


# ── заявка к модератору: слот потрачен ──────────────────────────────────────


async def test_sent_join_request_spends_the_slot() -> None:
    """`InviteRequestSent` — состоявшийся исходящий запрос, а не отказ.

    Telegram записал его независимо от исхода, поэтому слот НЕ возвращается.
    Возврат слота превращал суточный лимит из трёх запросов в двадцать четыре:
    через час попытка снова свободна, и следующая модерируемая группа отправляет
    ещё одну заявку.
    """
    db = seeded_invite_db("ModeratedAbCdEf1", "SecondAbCdEfGh12")
    client = FakeTelegram(
        invites={
            "ModeratedAbCdEf1": invite("Барахолка Нячанг"),
            "SecondAbCdEfGh12": invite("Нячанг аренда"),
        },
        join_errors={"ModeratedAbCdEf1": InviteRequestSentError(request=None)},
    )

    assert await joiner(db, client, now=NOON).join_next() is None

    # Слот остался занятым: событие в журнале есть и никуда не делось.
    assert len(db.events) == 1
    # Кандидат выброшен насовсем — исход известен точно.
    assert not any(row["key"] == "+ModeratedAbCdEf1" for row in db.candidates)
    assert db.rejects["+ModeratedAbCdEf1"] == REJECT_JOIN_REQUEST_SENT


async def test_ten_sent_requests_exhaust_the_day() -> None:
    """Ровно десять исходящих запросов в сутки, даже если все ушли в заявки.

    Это и есть регресс на посчитанные рецензентом 24 запроса вместо 10: пока слот
    возвращался, каждый час освобождал новую попытку.
    """
    hashes = tuple(f"Hash{number:02d}AbCdEfGhIjKl" for number in range(1, 12))
    db = seeded_invite_db(*hashes)
    client = FakeTelegram(
        invites={name: invite("Барахолка Нячанг") for name in hashes},
        join_errors={name: InviteRequestSentError(request=None) for name in hashes},
    )

    attempts = [
        await joiner(db, client, now=NOON + timedelta(hours=hour)).join_next() for hour in range(11)
    ]

    assert attempts == [None] * 11
    # Наружу ушло ровно десять запросов, одиннадцатый не состоялся.
    assert [call for call in client.calls if call.startswith("join_invite")] == [
        f"join_invite:{invite_hash}" for invite_hash in hashes[:10]
    ]
    assert len(db.events) == MAX_JOINS_PER_DAY
    # Одиннадцатый кандидат остался в очереди — он ни в чём не виноват.
    assert any(
        row["key"] == f"+{hashes[10]}" and row["status"] == "queued" for row in db.candidates
    )


# ── группа против вещания ───────────────────────────────────────────────────


@dataclass
class FakeChatFlags:
    """Сущность Telegram в объёме флагов, по которым определяется тип."""

    broadcast: bool = False
    megagroup: bool = False
    gigagroup: bool = False


@pytest.mark.parametrize(
    ("entity", "is_group", "what"),
    [
        (FakeChatFlags(), True, "обычная группа: ни одного флага нет"),
        (FakeChatFlags(megagroup=True), True, "супергруппа"),
        (FakeChatFlags(broadcast=True), False, "канал"),
        # Гигагруппа — супергруппа, переведённая в режим вещания: пишут
        # только администраторы, то есть объявлений от людей там столько же,
        # сколько в канале. Оба сочетания флагов проверяются: в разных версиях
        # схемы `megagroup` рядом с `gigagroup` ведёт себя по-разному.
        (FakeChatFlags(broadcast=True, gigagroup=True), False, "гигагруппа"),
        (
            FakeChatFlags(broadcast=True, megagroup=True, gigagroup=True),
            False,
            "гигагруппа с обоими флагами",
        ),
    ],
)
def test_broadcast_is_never_taken_for_a_group(
    entity: FakeChatFlags, is_group: bool, what: str
) -> None:
    """Вещание не группа: объявлений от людей в нём не бывает."""
    assert telegram_discover_convert.is_group(entity) is is_group, what


# --------------------------------------------------------------------------
# Три способа превысить десять вступлений в сутки. Каждый замерен прогоном по
# суткам, поэтому и закреплён отдельным тестом: суточный лимит — единственное,
# что отделяет рабочий аккаунт от бана.
# --------------------------------------------------------------------------


def outgoing_joins(client: FakeTelegram) -> int:
    """Сколько запросов на вступление УШЛО наружу — удачных и неудачных.

    Считать по `joined` нельзя: там только успехи, а суточный лимит Telegram
    считает запросы. Первая версия этих тестов мерила именно `joined` и потому
    пропускала ровно тот дефект, ради которого написана — возврат слота на
    отказе давал 24 запроса в сутки при трёх успехах.
    """
    return sum(1 for call in client.calls if call.startswith("join"))


def broken_client(error: Exception, *usernames: str) -> FakeTelegram:
    """Клиент, у которого вступление в любой чат кончается этой ошибкой."""
    client = joiner_client(*usernames)
    client.join_errors = {name: error for name in usernames}
    return client


async def test_a_refusal_does_not_hand_the_slot_back() -> None:
    """Отказ Telegram тратит слот: запрос наружу состоялся.

    Замер до правки: очередь из недоступных чатов, вызов раз в час — 24
    исходящих `JoinChannel` за сутки вместо трёх, потому что `release_slot`
    удалял событие вместе с паузой в час. Флуд-лимит Telegram считает запросы,
    а не успехи, и принцип «слот потрачен, если запрос состоялся» записан в
    spec-v2 4.5 прямым текстом.
    """
    names = tuple(f"gone{index}" for index in range(30))
    db = seeded_db(*names)
    client = broken_client(ChannelPrivateError(request=None), *names)

    for hour in range(24):
        await joiner(db, client, now=NOON + timedelta(hours=hour)).join_next()

    assert outgoing_joins(client) <= MAX_JOINS_PER_DAY, (
        f"наружу ушло {outgoing_joins(client)} запросов вместо {MAX_JOINS_PER_DAY}"
    )


async def test_an_eternal_candidate_stops_eating_the_daily_slots() -> None:
    """Кандидат с неизвестным исходом не крутится в очереди бесконечно.

    Замер до правки: `UserAlreadyParticipantError` уходил в общий `except`,
    кандидат возвращался в очередь, `reserve()` брал его же — 21 исходящий join
    за семь суток, по три в день, вечно, и второй кандидат не получал хода
    никогда. Считаются попытки, а не типы ошибок: так закрывается и седьмая
    ошибка, которую никто не назвал.
    """
    db = seeded_db("eternal", "second")
    client = broken_client(TimeoutError("обрыв связи посреди вступления"), "eternal", "second")

    for hour in range(7 * 24):
        await joiner(db, client, now=NOON + timedelta(hours=hour)).join_next()

    assert db.rejects.get("@eternal") == REJECT_TOO_MANY_ATTEMPTS
    assert not any(row["key"] == "@eternal" for row in db.candidates), (
        "вечный кандидат остался в очереди"
    )
    assert outgoing_joins(client) <= 2 * MAX_CANDIDATE_ATTEMPTS, (
        f"на двух кандидатов ушло {outgoing_joins(client)} запросов за семь суток"
    )


async def test_already_inside_spends_the_slot_and_drops_the_candidate() -> None:
    """«Мы уже внутри» — не повод пробовать снова: запрос ушёл, чат есть.

    Случай не редкий: очередь живёт неделями, и чат мог быть взят другим путём.
    """
    db = seeded_db("already")
    client = broken_client(UserAlreadyParticipantError(request=None), "already")

    assert await joiner(db, client, now=NOON).join_next() is None

    assert db.rejects.get("@already") == REJECT_ALREADY_INSIDE
    assert not any(row["key"] == "@already" for row in db.candidates)


async def test_a_full_account_stops_joins_like_a_flood() -> None:
    """Потолок чатов у самого Telegram: следующая попытка ничего не изменит.

    Пробовать ещё дважды в те же сутки — два бесполезных запроса в подпись
    автомата. Кандидат при этом не виноват и остаётся в очереди.
    """
    db = seeded_db("full", "next")
    broken = broken_client(ChannelsTooMuchError(request=None), "full", "next")

    assert await joiner(db, broken, now=NOON).join_next() is None

    healthy = joiner_client("full", "next")
    assert await joiner(db, healthy, now=NOON + timedelta(hours=2)).join_next() is None, (
        "после «аккаунт полон» стоп не действует"
    )
    assert any(row["key"] == "@full" and row["status"] == "queued" for row in db.candidates), (
        "кандидат ни в чём не виноват"
    )


async def test_a_flood_near_midnight_still_stops_for_half_a_day() -> None:
    """Флуд в 23:59 держал шестьдесят секунд — буква правила против его смысла.

    Замер до правки: `blocked_until` = полночь, то есть минута; дальше работала
    только часовая пауза, и в течение трёх часов после предупреждения уходило
    два новых вступления.
    """
    late = datetime(2026, 9, 1, 23, 59, tzinfo=UTC)
    db = seeded_db("flooded", "next")
    flooding = broken_client(FloodWaitError(request=None, capture=30), "flooded", "next")

    assert await joiner(db, flooding, now=late).join_next() is None

    healthy = joiner_client("flooded", "next")
    for hours in (1, 3, 6, 11):
        assert await joiner(db, healthy, now=late + timedelta(hours=hours)).join_next() is None, (
            f"через {hours} ч после флуда вступление прошло — стоп короче полусуток"
        )


# ── ответ на join без чата: id добирается чтением ───────────────────────────
#
# Живой отказ 01.09.2026: `JoinChannel` в @auto_moto_vietnam ответил без
# `chats`, клиент бросил `LookupError`, joiner счёл исход неизвестным и вернул
# кандидата в очередь — то есть через час ушёл ВТОРОЙ join в тот же чат. За
# сутки: два слота из десяти на один чат, `chats` пуст, чат не заглушен, вся
# очередь из 35 кандидатов стоит за первым. Ответ без чата бывает штатно
# (`UpdatesTooLong`), и вступление при этом состоялось.


class FakeTelethon:
    """Клиент, отвечающий по имени TL-запроса. Ничего, кроме диспетчера."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    async def __call__(self, request: Any) -> Any:
        name = type(request).__name__
        self.asked.append(name)
        answer = self.answers.get(name)
        if isinstance(answer, Exception):
            raise answer
        return answer


def a_channel(tg_id: int = 1234567890, *, username: str = "auto_moto_vietnam") -> Any:
    from telethon.tl.types import Channel

    return Channel(
        id=tg_id,
        title="Аренда Байков Нячанг",
        photo=None,
        date=None,
        megagroup=True,
        username=username,
    )


def a_user(tg_id: int = 777) -> Any:
    from telethon.tl.types import User as TgUser

    return TgUser(id=tg_id, first_name="человек", username="auto_moto_vietnam")


@dataclass
class Answer:
    """Ответ Telegram со списками сущностей — как их отдаёт `Updates`."""

    chats: list[Any] = field(default_factory=list)
    users: list[Any] = field(default_factory=list)


class UpdatesTooLongLike:
    """Ответ без `chats` вовсе — форма, на которой всё и сломалось."""


async def test_a_join_answer_without_chats_reads_the_id_instead_of_failing() -> None:
    """Вступление состоялось — значит и id обязан найтись, а не исход «неизвестен»."""
    channel = a_channel()
    client = FakeTelethon(
        {
            "JoinChannelRequest": UpdatesTooLongLike(),
            "ResolveUsernameRequest": Answer(chats=[channel]),
            "GetFullChannelRequest": None,
        }
    )

    tg_id = await telegram_discover_client.TelethonJoiner(client).join_public("@auto_moto_vietnam")

    assert tg_id == -1001234567890
    assert "ResolveUsernameRequest" in client.asked, "id обязан читаться, а не угадываться"
    assert client.asked.count("JoinChannelRequest") == 1, "второго вступления быть не должно"


async def test_reading_the_id_after_a_join_costs_no_second_action() -> None:
    """Добор id — только чтение: закрытый список действий CLAUDE.md не растёт."""
    client = FakeTelethon(
        {
            "JoinChannelRequest": UpdatesTooLongLike(),
            "ResolveUsernameRequest": Answer(chats=[a_channel()]),
            "GetFullChannelRequest": None,
        }
    )

    await telegram_discover_client.TelethonJoiner(client).join_public("@auto_moto_vietnam")

    assert [name for name in client.asked if name in ALLOWED_ACTIONS] == ["JoinChannelRequest"]


async def test_an_unreadable_join_stays_an_unknown_outcome() -> None:
    """Имя перестало разрешаться — id взять неоткуда, и врать про него нельзя."""
    client = FakeTelethon(
        {"JoinChannelRequest": UpdatesTooLongLike(), "ResolveUsernameRequest": Answer()}
    )

    with pytest.raises(LookupError):
        await telegram_discover_client.TelethonJoiner(client).join_public("@gone")


async def test_a_username_that_turned_into_a_person_is_not_a_chat_id() -> None:
    """`from_user` отдаёт сырой id человека — принять его за чат нельзя."""
    client = FakeTelethon(
        {
            "JoinChannelRequest": UpdatesTooLongLike(),
            "ResolveUsernameRequest": Answer(users=[a_user()]),
        }
    )

    with pytest.raises(LookupError):
        await telegram_discover_client.TelethonJoiner(client).join_public("@auto_moto_vietnam")


async def test_an_invite_answer_without_chats_reads_the_id_from_the_invite() -> None:
    """У закрытой группы имени нет, зато `CheckChatInvite` знает, что мы внутри."""

    class InviteAlready:
        chat = a_channel(999, username="")

    client = FakeTelethon(
        {
            "ImportChatInviteRequest": UpdatesTooLongLike(),
            "CheckChatInviteRequest": InviteAlready(),
            "GetFullChannelRequest": None,
        }
    )

    tg_id = await telegram_discover_client.TelethonJoiner(client).join_invite("GoOdHaShAbCdEf12")

    assert tg_id == -1000000000999
    assert client.asked.count("ImportChatInviteRequest") == 1


async def test_an_invite_that_did_not_let_us_in_stays_unknown() -> None:
    """`ChatInvite` без `chat` — мы снаружи. Тут исход и правда неизвестен."""

    class StillOutside:
        chat = None
        title = "Аренда Байков Нячанг"

    client = FakeTelethon(
        {"ImportChatInviteRequest": UpdatesTooLongLike(), "CheckChatInviteRequest": StillOutside()}
    )

    with pytest.raises(LookupError):
        await telegram_discover_client.TelethonJoiner(client).join_invite("GoOdHaShAbCdEf12")
