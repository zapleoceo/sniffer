"""Адаптер Chotot на зафиксированном ответе gateway.

Фикстура `fixtures/chotot_ad_listing.json` снята живым запросом 2026-08-29 к
`gateway.chotot.com/v1/public/ad-listing`: два байка из Кханьхоа (cg=2020) и
третьим — объявление без изображений. Третье из другой категории: формат
ответа у Chotot от категории не зависит, а байка без фотографий в выдаче не
нашлось ни на одной странице.

Личные поля (имя продавца, id аккаунта, координаты, адрес) заменены или
удалены — CLAUDE.md запрещает коммитить дампы с персональными данными. Всё,
что читает маппер, оставлено как пришло.

Сети здесь нет: httpx подменён `MockTransport`. Тест, который ходит на
Chotot, падает не когда сломан адаптер, а когда Chotot чихнул.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from sniffer.domain.passport import Category
from sniffer.sources.base import UnknownSourceError, get_source, registered_sources
from sniffer.sources.chotot import ChototSource, build_params

FIXTURE = Path(__file__).parent / "fixtures" / "chotot_ad_listing.json"

Handler = Callable[[httpx.Request], httpx.Response]


def payload() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def source(handler: Handler) -> ChototSource:
    return ChototSource(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def answering(body: Any) -> ChototSource:
    return source(lambda _: httpx.Response(200, json=body))


async def search_fixture() -> list[Any]:
    return await answering(payload()).search("winner", {"city": "nha_trang"})


async def test_price_comes_as_number_and_as_shown_to_human() -> None:
    items = await search_fixture()
    assert items[0].price_vnd == 13_000_000
    assert items[0].price_raw == "13.000.000 đ"


async def test_price_missing_is_none_not_zero() -> None:
    """Chotot не гарантирует ни одного поля, кроме list_id."""
    ad = payload()["ads"][0] | {}
    del ad["price"]
    ad["price_string"] = "Giá thỏa thuận"
    items = await answering({"ads": [ad]}).search("", {})
    assert items[0].price_vnd is None
    assert items[0].price_raw == "Giá thỏa thuận"


async def test_posted_at_comes_from_list_time_not_from_date() -> None:
    """В поле `date` лежит «2 giờ trước» — фраза, а не метка времени."""
    items = await search_fixture()
    assert items[0].posted_at == datetime.fromtimestamp(1787978973, UTC)
    assert items[0].raw["date"] == "2 giờ trước"


async def test_posted_at_survives_garbage_timestamp() -> None:
    ad = payload()["ads"][0] | {"list_time": "вчера"}
    items = await answering({"ads": [ad]}).search("", {})
    assert items[0].posted_at is None


async def test_url_is_built_from_list_id_not_ad_id() -> None:
    items = await search_fixture()
    assert items[0].url == "https://www.chotot.com/134413797.htm"
    assert items[0].raw["ad_id"] == 178447593


async def test_images_and_seller_and_text() -> None:
    items = await search_fixture()
    first = items[0]
    assert len(first.images) == 3
    assert all(url.startswith("https://cdn.chotot.com/") for url in first.images)
    assert first.title == "Honda Winner X 2021 ABS Đỏ đen"
    assert "winner x 2021" in first.text
    assert first.seller_name == "Nguyen Van A"
    assert first.source == "chotot"
    assert first.external_id == "134413797"


async def test_ad_without_images_maps_to_empty_list() -> None:
    items = await search_fixture()
    assert items[2].images == []


async def test_ad_stripped_of_optional_fields_still_maps() -> None:
    ad = {"list_id": 42}
    items = await answering({"ads": [ad]}).search("", {})
    assert len(items) == 1
    item = items[0]
    assert item.url == "https://www.chotot.com/42.htm"
    assert (item.title, item.text, item.price_raw, item.seller_name) == ("", "", "", "")
    assert item.price_vnd is None
    assert item.posted_at is None
    assert item.images == []


async def test_ad_without_list_id_is_skipped_but_siblings_survive() -> None:
    ads = payload()["ads"]
    broken = {k: v for k, v in ads[0].items() if k != "list_id"}
    items = await answering({"ads": [broken, ads[1]]}).search("", {})
    assert [item.external_id for item in items] == ["134411847"]


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(lambda _: httpx.Response(200, content=b"<html>502</html>"), id="broken_json"),
        pytest.param(
            lambda _: httpx.Response(400, json={"message": "invalid input"}), id="http_400"
        ),
        pytest.param(lambda _: httpx.Response(200, json={"ads": "нет"}), id="ads_not_a_list"),
        pytest.param(lambda _: httpx.Response(200, json=[1, 2]), id="payload_not_an_object"),
    ],
)
async def test_broken_answer_degrades_instead_of_raising(handler: Handler) -> None:
    adapter = source(handler)
    assert await adapter.search("winner", {}) == []
    assert adapter.degraded


async def test_network_error_degrades_instead_of_raising() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("сеть недоступна", request=request)

    adapter = source(refuse)
    assert await adapter.search("winner", {}) == []
    assert adapter.degraded


async def test_healthy_answer_leaves_source_not_degraded() -> None:
    adapter = answering(payload())
    await adapter.search("winner", {})
    assert not adapter.degraded


async def test_request_carries_region_and_category_to_the_wire() -> None:
    seen: dict[str, str] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json=payload())

    await source(capture).search("xe ga", {"category": Category.MOTORBIKE})
    assert seen["region_v2"] == "7044"
    assert seen["area_v2"] == "704401"
    assert seen["cg"] == "2020"
    assert seen["q"] == "xe ga"


def test_nha_trang_maps_to_khanh_hoa_and_city_area() -> None:
    built = build_params("", {"city": "nha_trang", "category": "motorbike"})
    assert built["region_v2"] == 7044
    assert built["area_v2"] == 704401
    assert built["cg"] == 2020


def test_area_can_be_widened_to_the_whole_province() -> None:
    """Cam Ranh — та же провинция и не тот город; расширение осознанное."""
    built = build_params("", {"city": "nha_trang", "area_v2": None})
    assert built["region_v2"] == 7044
    assert "area_v2" not in built


def test_limit_is_clamped_to_what_server_actually_returns() -> None:
    assert build_params("", {"limit": 1000})["limit"] == 50
    assert build_params("", {"offset": 50})["o"] == 50


def test_empty_query_is_not_sent() -> None:
    assert "q" not in build_params("   ", {})


def test_unknown_city_and_category_do_not_break_params() -> None:
    built = build_params("honda", {"city": "atlantis", "category": "submarine"})
    assert "region_v2" not in built
    assert "cg" not in built
    assert built["q"] == "honda"


def test_registry_finds_adapter_by_name() -> None:
    assert isinstance(get_source("chotot"), ChototSource)
    assert "chotot" in registered_sources()
    with pytest.raises(UnknownSourceError):
        get_source("avito")
