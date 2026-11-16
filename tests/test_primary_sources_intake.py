"""Первоисточник и дедуп на входе: коллектор, живой поиск, воронка, сборщик.

Без живой базы: репозитории подменены. Сценарии с Postgres — в
`test_primary_sources_db.py` (выполняются в CI).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Ns
from typing import Any, ClassVar, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.agent_app import collector_gateway
from sniffer.agent_app.collector_gateway import CollectorGateway, archive_url
from sniffer.agent_app.extraction import Original
from sniffer.collector.ingest import to_raw
from sniffer.db.repositories.collection_tasks import CollectionLease
from sniffer.domain.records import Chat, RawMessage
from sniffer.sources.telegram_mapping import to_item
from sniffer.worker import archive as archive_worker

NOW = datetime(2026, 11, 16, tzinfo=UTC)
CHAT = Chat(tg_id=-1001, title="Аренда", city="nha_trang", username="arenda_group")
TEXT = "Сдаётся студия в Нячанге, район Тхап Ба, 30 м2, вид на горы. Цена 6 млн/мес"


def message(id_: int = 7, text: str = TEXT, **extra: Any) -> Ns:
    fields: dict[str, object] = {
        "id": id_,
        "message": text,
        "date": NOW,
        "media": None,
        "grouped_id": None,
        "reply_to": None,
        "fwd_from": None,
    }
    return Ns(**{**fields, **extra})


def channel_forward(post: int, username: str | None) -> dict[str, Any]:
    return {
        "fwd_from": Ns(from_id=Ns(channel_id=777), channel_post=post),
        "forward": Ns(chat=Ns(username=username)),
    }


# ── коллектор: сырьё помнит первоисточник ───────────────────────────────────


def test_the_collector_stores_where_a_forward_came_from() -> None:
    forwarded = message(1, **channel_forward(55, "ArendaChannel"))
    (stored,) = to_raw(CHAT, [forwarded])

    assert (stored.origin_kind, stored.origin_msg_id, stored.origin_username) == (
        "forward",
        55,
        "ArendaChannel",
    )
    assert stored.origin_chat_tg_id == -1000000000777
    assert stored.identity_hash is not None


def test_the_collector_marks_pointers_and_own_posts() -> None:
    own, pointer = to_raw(CHAT, [message(1), message(2, "👉 https://t.me/other_chat/9")])

    assert own.origin_kind is None and own.origin_msg_id is None
    assert (pointer.origin_kind, pointer.origin_username, pointer.origin_msg_id) == (
        "pointer",
        "other_chat",
        9,
    )


# ── живой поиск: находка ведёт на первоисточник ─────────────────────────────


def test_a_live_forward_links_to_the_channel_post() -> None:
    item = to_item(CHAT, cast(Any, message(3, **channel_forward(55, "ArendaChannel"))))

    assert item is not None and item.url == "https://t.me/ArendaChannel/55"


def test_a_live_copy_without_an_original_is_not_a_finding() -> None:
    hidden = message(3, fwd_from=Ns(from_id=Ns(user_id=1), channel_post=None))

    assert to_item(CHAT, cast(Any, hidden)) is None
    assert to_item(CHAT, cast(Any, message(4, "bit.ly/xyz"))) is None


# ── воронка: копия без первоисточника не становится карточкой ───────────────


class _Recorder:
    calls: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, session: object) -> None:
        pass

    async def set_stage(self, ids: list[int], stage: str, **kwargs: Any) -> None:
        type(self).calls.append({"ids": ids, "stage": stage, **kwargs})


class _Chats:
    def __init__(self, session: object) -> None:
        pass

    async def get_by_tg_id(self, tg_id: int) -> Chat | None:
        return None


@pytest.mark.parametrize("kind", ["forward_hidden", "pointer"])
async def test_the_pipeline_rejects_a_copy_as_not_primary(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    _Recorder.calls = []
    monkeypatch.setattr(archive_worker, "RawMessageRepository", _Recorder)
    monkeypatch.setattr(archive_worker, "ChatRepository", _Chats)
    raw = RawMessage(
        id=5,
        chat_tg_id=-1001,
        msg_id=1,
        text=TEXT,
        text_hash="h",
        posted_at=NOW,
        origin_kind=kind,
    )

    await archive_worker.ArchivePipeline(now=lambda: NOW)._one(raw, cast(Any, None))

    assert _Recorder.calls == [
        {
            "ids": [5],
            "stage": "rejected",
            "gate_signals": {"reason": "not_primary", "origin_kind": kind},
        }
    ]


async def test_a_forward_without_a_name_borrows_it_from_the_registry() -> None:
    class Registry:
        async def get_by_tg_id(self, tg_id: int) -> Chat:
            return Chat(tg_id=tg_id, title="Канал", city="nha_trang", username="RegChannel")

    raw = RawMessage(
        chat_tg_id=-1001,
        msg_id=1,
        text=TEXT,
        text_hash="h",
        posted_at=NOW,
        origin_kind="forward",
        origin_chat_tg_id=-1000000000777,
        origin_msg_id=55,
    )

    named = await archive_worker._with_origin_name(raw, cast(Any, Registry()))

    assert named.origin_username == "RegChannel"


# ── сборщик каталога: дубль не идёт ни в staging, ни в публикацию ───────────

LEASE = CollectionLease(
    11,
    "trusted",
    {
        "city": "nha_trang",
        "category": "apartment",
        "deal_type": "rent_out",
        "sources": ["archive"],
    },
    1,
    NOW + timedelta(seconds=180),
)
FACTS = {
    "facts": {
        "city": "nha_trang",
        "category": "apartment",
        "deal_type": "rent_out",
        "active": True,
    },
    "evidence": [
        {"field": "city", "quote": "Нячанге"},
        {"field": "category", "quote": "студия"},
        {"field": "deal_type", "quote": "Сдаётся"},
        {"field": "active", "quote": "Сдаётся"},
    ],
}


def original(external_id: str, url: str, text: str = TEXT) -> Original:
    return Original("archive", external_id, url, text[:200], text, NOW, NOW)


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> Any:
    tasks = Ns(require_lease=AsyncMock())
    monkeypatch.setattr(collector_gateway, "CollectionTaskRepository", lambda _: tasks)

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, Ns(commit=AsyncMock()))

    return scope


async def test_copies_in_one_task_are_collected_once(sessions: Any) -> None:
    copies = [
        original("-1001:1", "https://t.me/a_group/1"),
        original("-1002:9", "https://t.me/b_group/9", "🔥 " + TEXT),  # кросспост
        original("-1003:4", "https://t.me/A_Group/1", "другой текст пересылки"),  # та же ссылка
    ]
    gateway = CollectorGateway(LEASE, sessions=sessions, fetch=AsyncMock(return_value=copies))

    result = await gateway.call("sources_collect", {"source": "archive"})

    assert result == {"count": 1, "source": "archive", "duplicates_skipped": 2}
    assert [item.external_id for item in gateway.originals] == ["-1001:1"]


async def test_an_already_published_advert_is_neither_staged_nor_published(
    sessions: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Ns(
        duplicate_of=AsyncMock(return_value="https://t.me/a_group/1"),
        stage=AsyncMock(),
        publish=AsyncMock(),
    )
    monkeypatch.setattr(collector_gateway, "CatalogObservationRepository", lambda _: repo)
    gateway = CollectorGateway(
        LEASE,
        sessions=sessions,
        fetch=AsyncMock(return_value=[original("-1002:9", "https://t.me/b_group/9")]),
    )
    await gateway.call("sources_collect", {"source": "archive"})

    result = await gateway.call("catalog_stage", {"index": 0, "extracted": FACTS})

    assert result == {
        "observation_id": None,
        "published": False,
        "duplicate_of": "https://t.me/a_group/1",
    }
    repo.stage.assert_not_awaited()
    repo.publish.assert_not_awaited()
    assert gateway.published == 0
    with pytest.raises(ValueError, match="repeated_original"):
        await gateway.call("catalog_stage", {"index": 0, "extracted": FACTS})


def test_an_archive_forward_links_to_its_original() -> None:
    row = {
        "username": "arenda_group",
        "msg_id": 7,
        "origin_kind": "forward",
        "origin_msg_id": 55,
        "origin_username": "ArendaChannel",
    }

    assert archive_url(row) == "https://t.me/ArendaChannel/55"
    assert archive_url({**row, "origin_kind": None}) == "https://t.me/arenda_group/7"
    with pytest.raises(ValueError, match="archive_origin_without_public_url"):
        archive_url({**row, "origin_username": None})


async def test_collector_retains_price_only_similar_flats(sessions: Any) -> None:
    from tests.test_listing_identity import FLAT

    copies = [
        original("-1001:1", "https://t.me/a_group/1", FLAT),
        original("-1002:2", "https://t.me/b_group/2", FLAT.replace("6 млн", "5 млн")),
    ]
    gateway = CollectorGateway(LEASE, sessions=sessions, fetch=AsyncMock(return_value=copies))
    result = await gateway.call("sources_collect", {"source": "archive"})
    assert result == {"count": 2, "source": "archive"}
