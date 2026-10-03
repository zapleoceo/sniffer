"""Догон карточек на живом Postgres: чтение страницы, запись патча, гонка с воркером.

Пропускается без `TEST_DATABASE_URL` (CI поднимает `pgvector/pgvector:pg16`).
Всё, что здесь проверяется, зависит от самого Postgres и на подделке было бы
зелёным при любой ошибке: `jsonb ||`, `IS NOT DISTINCT FROM` по `jsonb` и
`numeric`, SAVEPOINT после отказа `NUMERIC(14,2)`, перепроверка `WHERE` на
`READ COMMITTED`. Условие «строка не изменилась» отдельно и без базы проверено в
`test_enrich_guard.py`; здесь — что оно же верно на настоящей базе.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from sniffer.db import models
from sniffer.db.repositories import ListingRepository, RawMessageRepository
from sniffer.db.repositories.listing_enrichment import ListingEnrichmentRepository
from sniffer.domain.listing_patch import (
    ListingPatch,
    ListingWithText,
    WriteStatus,
)
from sniffer.domain.prices import PriceFact
from sniffer.domain.records import Listing, RawMessage
from sniffer.pipeline.enrich import derive
from sniffer.pipeline.enrich_price import PriceDerivation
from sniffer.worker import enrich as module
from sniffer.worker.enrich import EnrichPass, write_each
from sniffer.worker.enrich_report import END, EnrichReport
from tests.enrich_support import bounds_of, fact

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL не задан: живого Postgres нет",
)

# Настоящее «сейчас», а не зашитая дата: база сверяет время со своим `now()`.
NOW = datetime.now(UTC).replace(microsecond=0)
# Тексты-ключи: разбор подменён заглушкой, и политика проверяется на заданных
# фактах, а не на числах живого разбора (его правят в соседней ветке).
RENT = "fill"
LABELLED = "replace"
DIFFERENT = "disagree"
FACTS: dict[str, PriceFact | None] = {
    "fill": fact(12_500_000, period="month"),
    "replace": fact(5_500_000, period="month"),
    "disagree": fact(9_000_000, period="month"),
    "erase": None,
    "fill-sell": fact(21_000_000),
    "rate": fact(250_000, period="day"),
}


def parse(
    text: str, *, category: str | None = None, deal_type: str | None = None
) -> PriceFact | None:
    return FACTS.get(text)


def price_only(listing: Listing, text: str) -> ListingPatch:
    return derive(listing, text, derivations=(PriceDerivation(parse=parse, bounds_of=bounds_of),))


def never_by_verdict(listing: Listing, text: str) -> bool:
    return False


@asynccontextmanager
async def _borrowed(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Сессия теста вместо своей: проверяем запросы, а не сборку соединения."""
    yield session


async def _seed(
    session: AsyncSession, msg_id: int, text: str | None = RENT, **fields: Any
) -> Listing:
    """Карточка архива вместе с её сырьём; `text=None` — карточка без сырья."""
    raw_id = None
    if text is not None:
        (raw_id,) = await RawMessageRepository(session).add_many(
            [
                RawMessage(
                    chat_tg_id=-100123,
                    msg_id=msg_id,
                    text=text,
                    text_hash=f"hash-{msg_id}",
                    posted_at=NOW,
                )
            ]
        )
    base: dict[str, Any] = {
        "raw_message_id": raw_id,
        "deal_type": "rent_out",
        "category": "apartment",
        "city": "nha_trang",
        "title": f"Объявление {msg_id}",
        "summary": "сводка",
        "tg_link": f"https://t.me/c/1/{msg_id}",
        "posted_at": NOW,
        "external_id": f"-100123:{msg_id}",
    }
    stored = await ListingRepository(session).add(Listing(**{**base, **fields}))
    await session.commit()
    return stored


async def _row(session: AsyncSession, listing_id: int) -> models.Listing:
    """Строка такой, какая она в базе СЕЙЧАС, а не в памяти сессии."""
    session.expire_all()
    row = await session.get(models.Listing, listing_id)
    assert row is not None
    return row


def _snapshot(row: models.Listing) -> tuple[Any, ...]:
    return (
        row.price_amount,
        row.price_currency,
        row.price_period,
        row.attributes,
        row.is_active,
        row.screened_at,
        row.posted_at,
        row.deal_type,
        row.category,
    )


# ── чтение страницы ────────────────────────────────────────────────────────


async def test_a_page_is_active_telegram_cards_with_their_text_in_id_order(
    db_session: AsyncSession,
) -> None:
    first = await _seed(db_session, 1, RENT)
    await _seed(db_session, 2, RENT, is_active=False)
    await _seed(db_session, 3, RENT, source="chotot", external_id="chotot:3")
    no_text = await _seed(db_session, 4, None)
    last = await _seed(db_session, 5, LABELLED)

    page = await ListingEnrichmentRepository(db_session).page(
        "telegram_archive", after_id=0, limit=10
    )

    assert [item.listing.id for item in page] == [first.id, no_text.id, last.id]
    assert page[0].text == RENT
    assert page[1].text is None, "карточка без сырья не исчезает: она строка отчёта"
    assert page[2].text == LABELLED


async def test_a_page_respects_the_cursor_and_the_limit(db_session: AsyncSession) -> None:
    cards = [await _seed(db_session, n, RENT) for n in range(1, 6)]
    repo = ListingEnrichmentRepository(db_session)

    first = await repo.page("telegram_archive", after_id=0, limit=2)
    second = await repo.page("telegram_archive", after_id=first[-1].listing.id or 0, limit=2)

    assert [i.listing.id for i in first] == [cards[0].id, cards[1].id]
    assert [i.listing.id for i in second] == [cards[2].id, cards[3].id]


async def test_a_page_shows_what_the_database_has_now_not_what_the_session_remembers(
    db_session: AsyncSession,
) -> None:
    card = await _seed(db_session, 1, RENT)
    repo = ListingEnrichmentRepository(db_session)
    (before,) = await repo.page("telegram_archive", after_id=0, limit=10)
    # Мимо ORM-синхронизации: объект в памяти сессии остаётся прежним, как у сессии,
    # которую держат долго, — иначе тест не отличил бы свежее чтение от старого.
    await db_session.execute(
        update(models.Listing)
        .where(models.Listing.id == card.id)
        .values(title="Новый заголовок")
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()

    (after,) = await repo.page("telegram_archive", after_id=0, limit=10)

    assert before.listing.title != "Новый заголовок"
    assert after.listing.title == "Новый заголовок"


# ── запись патча ───────────────────────────────────────────────────────────


async def test_apply_writes_the_patch_and_merges_attributes_exactly_as_the_spec_says(
    db_session: AsyncSession,
) -> None:
    """`attributes || патч` в базе и слияние словарей в `applied_to` — одно и то же."""
    card = await _seed(
        db_session,
        1,
        attributes={"rooms": 2, "brand": "none", "furnished": True},
        price_amount=Decimal(5_500),
        price_currency="VND",
        price_period="month",
    )
    patch = ListingPatch(
        {"price_amount": Decimal(5_500_000), "district": "north"},
        {"price_up_to": 11_000_000, "rooms": 3},
    )

    assert await ListingEnrichmentRepository(db_session).apply(card, patch) is True
    await db_session.commit()

    stored = await _row(db_session, card.id or 0)
    expected = patch.applied_to(card)
    assert stored.price_amount == expected.price_amount == Decimal(5_500_000)
    assert stored.district == "north"
    assert stored.attributes == expected.attributes
    assert stored.attributes == {
        "rooms": 3,
        "brand": "none",
        "furnished": True,
        "price_up_to": 11_000_000,
    }, "чужие ключи целы, ключ патча главнее"


async def test_apply_removes_the_named_keys_and_merges_in_one_statement_like_the_spec(
    db_session: AsyncSession,
) -> None:
    """`(attributes - удаляемые) || патч` в базе и `applied_to` в памяти — одно и то же."""
    card = await _seed(
        db_session,
        1,
        attributes={"rate_amount": 250_000, "rate_per": "day", "brand": "honda", "rooms": 2},
    )
    patch = ListingPatch(
        {"price_amount": Decimal(21_000_000)},
        {"price_up_to": 25_000_000},
        remove=("rate_amount", "rate_per", "key_that_is_not_there"),
    )

    assert await ListingEnrichmentRepository(db_session).apply(card, patch) is True
    await db_session.commit()

    stored = await _row(db_session, card.id or 0)
    assert stored.attributes == patch.applied_to(card).attributes
    assert stored.attributes == {"brand": "honda", "rooms": 2, "price_up_to": 25_000_000}


async def test_a_patch_that_only_removes_keys_leaves_the_rest_of_the_attributes(
    db_session: AsyncSession,
) -> None:
    card = await _seed(db_session, 1, attributes={"rate_per": "day", "rooms": 2})

    written = await ListingEnrichmentRepository(db_session).apply(
        card, ListingPatch(remove=("rate_per",))
    )
    await db_session.commit()

    assert written is True
    assert (await _row(db_session, card.id or 0)).attributes == {"rooms": 2}


async def test_apply_can_empty_the_price_columns(db_session: AsyncSession) -> None:
    """Стирание — запись NULL, и охрана по уже пустым колонкам тоже обязана пройти."""
    card = await _seed(
        db_session,
        1,
        price_amount=Decimal(5_000_000_000),
        price_currency="VND",
        price_period="month",
    )

    written = await ListingEnrichmentRepository(db_session).apply(
        card,
        ListingPatch({"price_amount": None, "price_currency": None, "price_period": None}),
    )
    await db_session.commit()

    stored = await _row(db_session, card.id or 0)
    assert written is True
    assert (stored.price_amount, stored.price_currency, stored.price_period) == (None, None, None)


async def test_apply_never_touches_the_life_or_the_identity_of_a_card(
    db_session: AsyncSession,
) -> None:
    card = await _seed(db_session, 1)
    await db_session.execute(
        update(models.Listing).where(models.Listing.id == card.id).values(screened_at=NOW)
    )
    await db_session.commit()
    before = _snapshot(await _row(db_session, card.id or 0))
    fresh = (
        await ListingEnrichmentRepository(db_session).page("telegram_archive", after_id=0, limit=1)
    )[0]

    written = await ListingEnrichmentRepository(db_session).apply(
        fresh.listing, ListingPatch({"price_amount": Decimal(12_500_000)}, {"x": 1})
    )
    await db_session.commit()

    assert written is True, "строка не менялась — запись обязана состояться"

    after = _snapshot(await _row(db_session, card.id or 0))
    # Меняются цена и атрибуты; жизнь и личность карточки — прежние.
    assert after[4:] == before[4:], "is_active, screened_at, posted_at, deal_type, category"
    assert after[0] == Decimal(12_500_000) and after[3] == {"x": 1}


# Что мог изменить живой воркер, пока мы считали (по колонке на строку).
CONCURRENT: dict[str, Any] = {
    "raw_message_id": None,
    "category": "motorbike",
    "deal_type": "sell",
    "city": "da_nang",
    "price_amount": Decimal(123),
    "price_currency": "USD",
    "price_period": "once",
    "district": "north",
    "title": "Другой заголовок",
    "lang": "vi",
    "attributes": {"rooms": 9},
}


@pytest.mark.parametrize("column", sorted(CONCURRENT))
async def test_a_row_changed_since_the_read_is_not_written(
    db_session: AsyncSession, column: str
) -> None:
    card = await _seed(
        db_session,
        1,
        attributes={"rooms": 2},
        price_amount=Decimal(9_000_000),
        price_currency="VND",
        price_period="month",
        district="center",
        lang="ru",
    )
    read = (
        await ListingEnrichmentRepository(db_session).page("telegram_archive", after_id=0, limit=1)
    )[0]
    await db_session.execute(
        update(models.Listing)
        .where(models.Listing.id == card.id)
        .values({column: CONCURRENT[column]})
    )
    await db_session.commit()

    written = await ListingEnrichmentRepository(db_session).apply(
        read.listing, ListingPatch({"price_amount": Decimal(1_000_000)})
    )
    await db_session.commit()

    assert written is False
    stored = await _row(db_session, card.id or 0)
    assert stored.price_amount != Decimal(1_000_000), "по устаревшему решению не пишем"
    assert getattr(stored, column) == CONCURRENT[column], "чужая правка цела"


async def test_a_refusal_of_the_database_costs_one_card_and_the_batch_goes_on(
    db_session: AsyncSession,
) -> None:
    """Живой отказ 01.09.2026: число, не влезающее в `NUMERIC(14,2)`, не должно ронять пачку."""
    cards = [await _seed(db_session, n) for n in (1, 2, 3)]
    repo = ListingEnrichmentRepository(db_session)
    items = [
        (ListingWithText(cards[0], RENT), ListingPatch({"price_amount": Decimal(1_000_000)})),
        (ListingWithText(cards[1], RENT), ListingPatch({"price_amount": Decimal(10**13)})),
        (ListingWithText(cards[2], RENT), ListingPatch({"price_amount": Decimal(3_000_000)})),
    ]

    results = await write_each(repo, items)
    await db_session.commit()

    assert [r.status for r in results] == [
        WriteStatus.APPLIED,
        WriteStatus.FAILED,
        WriteStatus.APPLIED,
    ]
    assert results[1].error and str(10**13) not in results[1].error
    assert (await _row(db_session, cards[0].id or 0)).price_amount == Decimal(1_000_000)
    assert (await _row(db_session, cards[1].id or 0)).price_amount is None
    assert (await _row(db_session, cards[2].id or 0)).price_amount == Decimal(3_000_000)


# ── проход целиком ─────────────────────────────────────────────────────────


async def _pass(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, *, dry_run: bool = False
) -> EnrichReport:
    monkeypatch.setattr(module, "session_scope", lambda: _borrowed(db_session))
    report = EnrichReport()
    await EnrichPass(size=2, derive_row=price_only, by_verdict=never_by_verdict).run(
        report, dry_run=dry_run
    )
    return report


async def test_the_pass_fills_replaces_counts_and_leaves_the_rest_alone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    filled = await _seed(db_session, 1, RENT)
    replaced = await _seed(
        db_session,
        2,
        LABELLED,
        price_amount=Decimal(5_500),
        price_currency="VND",
        price_period="month",
    )
    disagree = await _seed(
        db_session,
        3,
        DIFFERENT,
        price_amount=Decimal(7_000_000),
        price_currency="VND",
        price_period="month",
    )
    inactive = await _seed(db_session, 4, RENT, is_active=False)
    other_source = await _seed(db_session, 5, RENT, source="chotot", external_id="chotot:5")

    report = await _pass(db_session, monkeypatch)

    assert report.stop_reason == END
    assert (await _row(db_session, filled.id or 0)).price_amount == Decimal(12_500_000)
    assert (await _row(db_session, replaced.id or 0)).price_amount == Decimal(5_500_000)
    assert (await _row(db_session, disagree.id or 0)).price_amount == Decimal(7_000_000)
    assert (await _row(db_session, inactive.id or 0)).price_amount is None, "погашенные не трогаем"
    assert (await _row(db_session, other_source.id or 0)).price_amount is None, "и чужой источник"
    assert report.totals()["price.filled"] == 1
    assert report.totals()["price.replaced"] == 1
    assert report.totals()["price.disagreed"] == 1
    assert report.totals()["written"] == 2


async def test_a_second_pass_over_a_real_database_writes_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [
        (await _seed(db_session, 1, RENT)).id or 0,
        (await _seed(db_session, 2, "rate", category="motorbike")).id or 0,
    ]
    first = await _pass(db_session, monkeypatch)
    after_first = [_snapshot(await _row(db_session, i)) for i in ids]

    second = await _pass(db_session, monkeypatch)

    assert first.totals()["written"] == 2, "цена первой, суточная ставка во второй"
    assert second.totals()["written"] == 0
    assert [_snapshot(await _row(db_session, i)) for i in ids] == after_first


async def test_a_dry_run_leaves_the_database_exactly_as_it_was(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = await _seed(db_session, 1, RENT)
    before = _snapshot(await _row(db_session, card.id or 0))

    report = await _pass(db_session, monkeypatch, dry_run=True)

    assert report.totals()["would_write"] == 1 and report.totals()["written"] == 0
    assert _snapshot(await _row(db_session, card.id or 0)) == before


async def test_a_card_changed_by_the_worker_during_the_pass_is_skipped_and_its_change_survives(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Гонка, ради которой запись — сравнение с обменом: воркер сменил сторону сделки."""
    card = await _seed(db_session, 1, RENT)
    repo = ListingEnrichmentRepository(db_session)

    async def racing_page(after_id: int, limit: int) -> list[ListingWithText]:
        rows = await repo.page("telegram_archive", after_id=after_id, limit=limit)
        # Между чтением и записью проверка модели успела признать карточку продажей.
        await db_session.execute(
            update(models.Listing)
            .where(models.Listing.id == card.id)
            .values(deal_type="sell", price_period="once")
        )
        await db_session.commit()
        return rows

    monkeypatch.setattr(module, "session_scope", lambda: _borrowed(db_session))
    report = EnrichReport()
    await EnrichPass(
        page=racing_page, derive_row=price_only, by_verdict=never_by_verdict, size=5
    ).run(report)

    stored = await _row(db_session, card.id or 0)
    assert report.totals()["skip.changed_since_read"] == 1
    assert report.totals()["written"] == 0
    assert stored.price_amount is None, "цена по устаревшему решению не записана"
    assert (stored.deal_type, stored.price_period) == ("sell", "once"), "правка воркера цела"


async def test_the_pass_erases_an_implausible_price_and_cleans_the_rate_of_the_former_side(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    erased = await _seed(
        db_session,
        1,
        "erase",
        price_amount=Decimal(5_000_000_000),
        price_currency="VND",
        price_period="month",
        attributes={"price_up_to": 6_000_000_000, "rooms": 3},
    )
    flipped = await _seed(
        db_session,
        2,
        "fill-sell",
        category="motorbike",
        deal_type="sell",
        attributes={"rate_amount": 250_000, "rate_per": "day", "brand": "honda"},
    )

    report = await _pass(db_session, monkeypatch)

    first = await _row(db_session, erased.id or 0)
    second = await _row(db_session, flipped.id or 0)
    assert report.totals()["price.erased"] == 1 and report.totals()["price.filled"] == 1
    assert (first.price_amount, first.price_currency, first.price_period) == (None, None, None)
    assert first.attributes == {
        "rooms": 3,
        "price_erased": {"amount": "5000000000.00", "currency": "VND", "period": "month"},
    }, "верх вилки ушёл вместе с ценой, чужое цело, а стёртая цена осталась следом"
    assert second.price_amount == Decimal(21_000_000) and second.price_period == "once"
    assert second.attributes == {"brand": "honda"}, "ставка прежней стороны не пережила смену"


async def test_the_pass_with_every_real_default_reads_a_label_and_fills_the_price(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Провод целиком: настоящий разбор, настоящий реестр, настоящая воронка для пометки."""
    card = await _seed(db_session, 1, "Oceanus, 2 спальни.\nАрендная плата: 12.5 млн VND / месяц")
    monkeypatch.setattr(module, "session_scope", lambda: _borrowed(db_session))
    report = EnrichReport()

    await EnrichPass().run(report)

    assert (await _row(db_session, card.id or 0)).price_amount == Decimal(12_500_000)
    assert report.totals()["price.filled"] == 1
