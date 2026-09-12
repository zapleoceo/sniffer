"""Обход Chotot по расписанию: фикстура доски → карточки каталога, без сети.

Gateway подменён `httpx.MockTransport` на той же фикстуре, что у адаптера
(`fixtures/chotot_ad_listing.json`). Проверяется расписание, страницы, перевод
структурных полей доски в атрибуты карточки и что лежащая доска не роняет
воркер и ничего не пишет.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from sniffer.domain.passport import Category
from sniffer.domain.records import Listing
from sniffer.sources.base import RawItem
from sniffer.sources.chotot import ChototSource
from sniffer.sources.chotot_reference import CATEGORY_CG, REGION_V2
from sniffer.worker.chotot_sync import ChototSync, cities, listing_from_ad, structured_facts

FIXTURE = Path(__file__).parent / "fixtures" / "chotot_ad_listing.json"


def payload() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


@dataclass
class Board:
    """Gateway Chotot из фикстуры: считает запросы и умеет лечь."""

    requests: list[dict[str, str]] = field(default_factory=list)
    broken: bool = False
    ads: list[dict[str, Any]] | None = None

    def source(self) -> ChototSource:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(dict(request.url.params))
            if self.broken:
                return httpx.Response(503)
            return httpx.Response(200, json={"ads": self.ads or payload()["ads"]})

        return ChototSource(client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))


@dataclass
class Store:
    rows: list[Listing] = field(default_factory=list)

    async def __call__(self, listings: list[Listing]) -> int:
        self.rows.extend(listings)
        return len(listings)


def sync(board: Board, store: Store, *, clock: Any, interval: float = 1800.0) -> ChototSync:
    return ChototSync(interval_s=interval, source_factory=board.source, store=store, clock=clock)


async def test_first_pass_runs_at_start_then_waits_for_the_interval() -> None:
    board, store = Board(), Store()
    now = [1000.0]
    job = sync(board, store, clock=lambda: now[0])

    assert await job.tick() > 0
    assert await job.tick() == 0, "второй проход раньше срока — лишний запрос к гостям"
    now[0] += 1800.0
    assert await job.tick() > 0


async def test_each_served_city_and_board_category_gets_its_newest_page() -> None:
    board, store = Board(), Store()
    await sync(board, store, clock=lambda: 0.0).tick()

    # Фикстура отдаёт три объявления — меньше страницы, вторую не листаем.
    assert len(board.requests) == len(cities()) * len(CATEGORY_CG)
    assert all(request["o"] == "0" and request["limit"] == "50" for request in board.requests)
    assert {request["cg"] for request in board.requests} == {"2020"}
    assert {request["region_v2"] for request in board.requests} == {
        str(REGION_V2[city]) for city in cities()
    }


async def test_ads_become_catalog_listings_of_the_board() -> None:
    board, store = Board(), Store()
    await sync(board, store, clock=lambda: 0.0).tick()

    rows = [row for row in store.rows if row.city == "nha_trang"]
    assert len(rows) == 3
    first = rows[0]
    assert (first.source, first.external_id) == ("chotot", "134413797")
    assert (first.deal_type, first.category) == ("sell", "motorbike")
    assert first.price_amount == 13_000_000
    assert (first.price_currency, first.price_period) == ("VND", "once")
    assert first.tg_link == "https://www.chotot.com/134413797.htm"
    assert first.posted_at == datetime.fromtimestamp(1787978973, UTC)
    assert first.raw_message_id is None
    assert first.confidence > 0.55, "цена и категория здесь — поля доски, не догадка"


def test_board_fields_win_over_words_of_the_text() -> None:
    """Продавец выбрал тип кузова из списка — это точнее слова в описании."""
    item = RawItem(
        source="chotot",
        external_id="7",
        url="https://www.chotot.com/7.htm",
        title="Honda Winner X 2020",
        text="Xe côn tay 150cc, giấy tờ đầy đủ",
        price_vnd=30_000_000,
        posted_at=datetime(2026, 9, 12, tzinfo=UTC),
        raw={"motorbiketype": 1, "motorbikebrand": 2, "regdate": 2020},
    )
    listing = listing_from_ad(item, city="nha_trang", category=Category.MOTORBIKE)

    assert listing.attributes["model"] == "winner"
    assert listing.attributes["engine_cc"] == 150
    # Текст говорит «côn tay» (механика), поле доски — вариатор: побеждает поле.
    assert listing.attributes["transmission"] == "automatic"
    assert listing.attributes["body_type"] == "tay_ga"
    assert listing.attributes["brand"] == "yamaha"
    assert listing.attributes["year"] == 2020


def test_structured_facts_read_only_known_codes() -> None:
    assert structured_facts({"motorbiketype": 3, "motorbikebrand": 3}) == {
        "transmission": "manual",
        "brand": "piaggio",
    }
    assert structured_facts({"motorbiketype": "2", "regdate": "2019"}) == {
        "transmission": "semi",
        "year": 2019,
    }
    # Электро: ни коробки, ни вариатора — поле честно пусто.
    assert structured_facts({"motorbiketype": 4, "motorbikebrand": 999}) == {}
    assert structured_facts({"motorbiketype": "ga", "regdate": 3000}) == {}


async def test_a_dead_board_stores_nothing_and_does_not_raise() -> None:
    board, store = Board(broken=True), Store()

    assert await sync(board, store, clock=lambda: 0.0).tick() == 0

    assert store.rows == []
    assert len(board.requests) == len(cities()) * len(CATEGORY_CG), "лежащую доску не листают"


async def test_undated_ads_never_enter_the_catalog() -> None:
    ads = payload()["ads"]
    ads[0] = dict(ads[0], list_time="вчера")
    board, store = Board(ads=ads), Store()

    await sync(board, store, clock=lambda: 0.0).tick()

    assert "134413797" not in {row.external_id for row in store.rows}
    assert store.rows, "остальные объявления доски доехали"
