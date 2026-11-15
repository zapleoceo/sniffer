"""Первоисточники и дедуп на живом Postgres (без `TEST_DATABASE_URL` пропускаются)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db import models
from sniffer.db.repositories.catalog_observations import CatalogObservationRepository
from sniffer.db.repositories.chats import ChatRepository
from sniffer.db.repositories.collection_sources import CollectionSourceRepository
from sniffer.db.repositories.collection_tasks import CollectionTaskRepository
from sniffer.db.repositories.raw_messages import RawMessageRepository
from sniffer.domain.catalog import CatalogFacts, CatalogObservation, Evidence
from sniffer.domain.fingerprint import fingerprint
from sniffer.domain.listing_identity import identity_hash
from sniffer.domain.passport import Category
from sniffer.domain.records import Chat, RawMessage
from sniffer.worker.archive import ArchivePipeline

NOW = datetime.now(UTC).replace(microsecond=0)
TEXT = (
    "Сдаётся студия в Нячанге, район Тхап Ба, 30 м2, вид на горы, мебель, кондиционер. "
    "Цена 6 млн/мес. Тел 0901234567"
)
SIGNALS = {
    "has_price": True,
    "is_offer": True,
    "is_demand": False,
    "categories": ["apartment"],
    "reason": "ok",
}


async def chats(session: AsyncSession) -> None:
    repo = ChatRepository(session)
    await repo.add(Chat(tg_id=-1001, title="A", city="nha_trang", username="a_group"))
    await repo.add(Chat(tg_id=-1002, title="B", city="nha_trang", username="b_group"))


def raw(chat: int, msg: int, hours: int, text: str = TEXT, **extra: object) -> RawMessage:
    return RawMessage(
        chat_tg_id=chat,
        msg_id=msg,
        text=text,
        text_hash=fingerprint(text),
        identity_hash=identity_hash(text),
        posted_at=NOW - timedelta(hours=hours),
        **extra,  # type: ignore[arg-type]
    )


async def test_the_archive_reads_only_primary_sources(db_session: AsyncSession) -> None:
    await chats(db_session)
    other = "Сдам дом в Нячанге у моря, 3 спальни, бассейн, 25 млн в месяц, без комиссии"
    ids = await RawMessageRepository(db_session).add_many(
        [
            raw(-1001, 1, 5),
            raw(-1002, 2, 4, origin_kind="forward_hidden"),
            raw(-1002, 3, 3, "Сдаётся 👉 https://t.me/x_chat/9", origin_kind="pointer"),
            raw(
                -1002,
                4,
                2,
                other,
                origin_kind="forward",
                origin_chat_tg_id=-1009,
                origin_msg_id=55,
                origin_username="SeaChannel",
            ),
        ]
    )
    await RawMessageRepository(db_session).set_stage(ids, "extracted", gate_signals=SIGNALS)
    await db_session.commit()

    rows = await CollectionSourceRepository(db_session).archive(
        {"city": "nha_trang", "category": "apartment", "deal_type": "rent_out", "criteria": {}},
        limit=6,
    )

    assert {(row["msg_id"], row["origin_kind"], row["origin_username"]) for row in rows} == {
        (1, None, None),
        (4, "forward", "SeaChannel"),
    }


async def test_a_repost_with_a_new_price_keeps_the_primary_link(
    db_session: AsyncSession,
) -> None:
    await chats(db_session)
    raws = RawMessageRepository(db_session)
    await raws.add_many([raw(-1001, 1, 48)])
    await db_session.commit()
    first = await raws.get_by_key(-1001, 1)
    assert first is not None
    assert await ArchivePipeline()._one(first, db_session) == 1
    await db_session.commit()

    await raws.add_many([raw(-1002, 7, 1, TEXT.replace("6 млн", "5,5 млн"))])
    await db_session.commit()
    repost = await raws.get_by_key(-1002, 7)
    assert repost is not None
    assert await ArchivePipeline()._one(repost, db_session) == 1
    await db_session.commit()

    cards = (await db_session.scalars(select(models.Listing))).all()
    assert len(cards) == 1
    assert cards[0].tg_link == "https://t.me/a_group/1"
    assert cards[0].price_amount == Decimal(5_500_000)
    stored = await raws.get_by_key(-1002, 7)
    assert stored is not None and stored.stage == "duplicate"


def chotot(external_id: str, url: str) -> CatalogObservation:
    text = "Нячанг. Продам скутер Honda Vision 2021, отличное состояние. Цена 100 VND. В наличии."
    quotes = {
        "city": "Нячанг",
        "category": "скутер",
        "deal_type": "Продам",
        "price_vnd": "100",
        "active": "В наличии",
    }
    return CatalogObservation(
        source="chotot",
        external_id=external_id,
        url=url,
        fetched_at=datetime.now(UTC) - timedelta(seconds=30),
        title="Скутер",
        raw_text=text,
        extractor_version="test-v1",
        facts=CatalogFacts(
            city="nha_trang",
            category=Category.MOTORBIKE,
            deal_type="sell",
            price_vnd=100,
            active=True,
        ),
        evidence=tuple(Evidence(field=f, quote=q) for f, q in quotes.items()),  # type: ignore[arg-type]
    )


async def test_a_second_copy_of_a_published_advert_is_reported_as_duplicate(
    db_session: AsyncSession,
) -> None:
    tasks = CollectionTaskRepository(db_session)
    task_id = await tasks.enqueue(
        {"city": "nha_trang", "category": "motorbike", "deal_type": "sell", "sources": ["chotot"]},
        user_id=1,
        request_id=1,
        request_version=1,
        window_key="hour",
    )
    lease = await tasks.claim()
    assert lease is not None and lease.id == task_id
    repo = CatalogObservationRepository(db_session)
    first = chotot("42", "https://www.chotot.com/42.htm")
    assert await repo.duplicate_of(first) is None
    assert await repo.publish(task_id, lease.token, await repo.stage(task_id, lease.token, first))
    await db_session.commit()

    # Тот же адрес под другим номером — копия; повтор того же номера — не копия.
    copy = chotot("43", "https://www.chotot.com/42.htm")
    assert await repo.duplicate_of(copy) == "https://www.chotot.com/42.htm"
    assert await repo.duplicate_of(first) is None
    assert len(await repo.search(city="nha_trang", category="motorbike")) == 1
