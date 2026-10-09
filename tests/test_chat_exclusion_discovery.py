"""Исключённая группа не возвращается в работу через разведку и вступление (020).

Фейки те же, что в `test_telegram_discover.py`: «диск» `FakeDb`, где исключённый чат остаётся
строкой реестра, но выпадает из потолка. Сети нет, а любой лишний вызов Telegram — падение.
"""

from __future__ import annotations

import structlog

from sniffer.sources.telegram_discover_reference import (
    REJECT_EXCLUDED,
    DiscoveredChat,
)
from tests.test_telegram_discover import (
    CITY,
    NOON,
    FakeDb,
    FakeMessage,
    FakeRegistry,
    FakeTelegram,
    discovery,
    group,
    invite,
    joiner,
    joiner_client,
    seeded_db,
    seeded_invite_db,
)

EXCLUDED_ID = -4242


def excluded_db(username: str = "dead_barakholka") -> FakeDb:
    db = FakeDb()
    db.chats[EXCLUDED_ID] = DiscoveredChat(EXCLUDED_ID, username, "Мёртвая барахолка", CITY)
    db.excluded.add(EXCLUDED_ID)
    return db


async def test_a_link_to_an_excluded_chat_makes_no_candidate_and_asks_telegram_nothing() -> None:
    db = excluded_db()
    client = FakeTelegram()

    with structlog.testing.capture_logs() as logs:
        added = await discovery(db, client).harvest(
            [FakeMessage(1, "t.me/dead_barakholka — тут пусто")], found_in="@other"
        )

    assert added == 0
    assert db.candidates == []
    assert client.calls == []
    assert [entry["event"] for entry in logs if entry["event"].startswith("discover.")] == [
        "discover.candidate_excluded"
    ]


async def test_an_excluded_chat_known_only_by_id_is_caught_after_the_resolve() -> None:
    """В строке реестра нет имени (вступали по приглашению): узнаём по tg_id из resolve."""
    db = excluded_db(username="")
    client = FakeTelegram(
        known={"renamed_chat": group("renamed_chat", "Нячанг барахолка", tg_id=EXCLUDED_ID)}
    )

    with structlog.testing.capture_logs() as logs:
        added = await discovery(db, client).harvest([FakeMessage(1, "t.me/renamed_chat")])

    assert added == 0 and db.candidates == []
    assert db.rejects == {}  # исключение обратимо: в отклонённые не пишем
    assert any(entry["event"] == "discover.candidate_excluded" for entry in logs)


async def test_a_candidate_queued_before_the_exclusion_is_dropped_and_never_joined() -> None:
    db = seeded_db("dead_barakholka", "alive_chat")
    db.chats[EXCLUDED_ID] = DiscoveredChat(EXCLUDED_ID, "dead_barakholka", "Мёртвая", CITY)
    db.excluded.add(EXCLUDED_ID)
    client = joiner_client("dead_barakholka", "alive_chat")

    with structlog.testing.capture_logs() as logs:
        chat = await joiner(db, client, now=NOON).join_next()

    assert chat is not None and chat.username == "alive_chat"
    assert client.joined == ["alive_chat"]  # в исключённый вступления не было
    assert db.rejects["@dead_barakholka"] == REJECT_EXCLUDED
    assert not any(row["key"] == "@dead_barakholka" for row in db.candidates)
    assert any(entry["event"] == "discover.candidate_excluded" for entry in logs)


async def test_joining_by_invite_into_an_excluded_chat_does_not_reactivate_or_duplicate() -> None:
    """Приглашение не покажет, чей чат, до вступления: страхует проверка после него."""
    db = seeded_invite_db("AbCdEfGhIjKlMnOpQr")
    joined_id = -1001111111111  # что отдаёт FakeTelegram.join_invite
    db.chats[joined_id] = DiscoveredChat(joined_id, "", "Исключённый по приглашению", CITY)
    db.excluded.add(joined_id)
    client = FakeTelegram(invites={"AbCdEfGhIjKlMnOpQr": invite("Барахолка Нячанг")})

    with structlog.testing.capture_logs() as logs:
        chat = await joiner(db, client, now=NOON).join_next()

    assert chat is None
    assert list(db.chats) == [joined_id]  # ни второй строки, ни новой активации
    assert joined_id in db.excluded
    assert client.muted == []  # настройки чужого для реестра чата не трогаем
    assert db.candidates == []
    assert any(entry["event"] == "discover.candidate_excluded" for entry in logs)


async def test_the_cap_does_not_count_excluded_chats() -> None:
    """199 в работе + 3 исключённых при потолке 200: место есть. 200 в работе: места нет."""
    db = FakeDb()
    for number in range(199):
        db.chats[-number - 1] = DiscoveredChat(-number - 1, f"chat{number}", "чат", CITY)
    for number in range(3):
        tg_id = -10_000 - number
        db.chats[tg_id] = DiscoveredChat(tg_id, f"dead{number}", "исключён", CITY)
        db.excluded.add(tg_id)
    assert len(db.chats) == 202
    assert await FakeRegistry(db).count() == 199

    db.candidates.extend(seeded_db("newcomer").candidates)
    client = joiner_client("newcomer")
    chat = await joiner(db, client, now=NOON, max_tracked=200).join_next()
    assert chat is not None and chat.username == "newcomer"

    # Двухсотый занял последнее место: следующий кандидат упирается в потолок.
    db.candidates.extend(
        {**row, "key": "@one_more", "username": "one_more", "seq": 99}
        for row in seeded_db("one_more").candidates
    )
    refused = await joiner(
        db, joiner_client("one_more"), now=NOON.replace(hour=15), max_tracked=200
    ).join_next()
    assert refused is None
