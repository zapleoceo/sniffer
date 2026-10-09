"""Класс кандидата по превью: фикстуры HTML, без сети."""

from __future__ import annotations

import httpx
import pytest

from sniffer.domain.chat_preview import (
    FOREIGN_CITY,
    OFF_TOPIC,
    RELEVANT,
    UNKNOWN,
    PreviewSnapshot,
    parse_preview,
)
from sniffer.search.chat_preview import classify_preview
from sniffer.sources.chat_preview_fetch import fetch_preview


def page(title: str, description: str = "", members: str = "1 200 members") -> str:
    meta = f'<meta property="og:title" content="{title}">'
    if description:
        meta += f'<meta property="og:description" content="{description}">'
    return f'<html><head>{meta}</head><div class="tgme_page_extra">{members}</div></html>'


def verdict(title: str, description: str = "") -> tuple[str, str]:
    result = classify_preview(parse_preview(page(title, description)))
    return result.cls, result.evidence


def test_market_words_in_description_make_it_relevant() -> None:
    cls, evidence = verdict("Нячанг чат", "Аренда квартир и байков в Нячанге")
    assert cls == RELEVANT
    assert "аренд" in evidence


def test_market_word_in_title_alone_is_relevant() -> None:
    assert verdict("Барахолка Нячанг")[0] == RELEVANT


def test_described_other_topic_without_market_words_is_off_topic() -> None:
    cls, evidence = verdict("Nha Trang Yoga", "Йога и пилатес для всех, занятия на пляже")
    assert cls == OFF_TOPIC
    assert "йог" in evidence and "рыночных слов нет" in evidence


def test_off_topic_needs_a_description_title_alone_never_rejects() -> None:
    assert verdict("Visa Run Group")[0] == UNKNOWN


def test_no_description_and_no_page_are_unknown() -> None:
    assert verdict("Просто чат")[0] == UNKNOWN
    assert classify_preview(parse_preview("<html></html>")).cls == UNKNOWN
    assert parse_preview(page("Telegram: Contact @x")).status == "no_page"


def test_ambiguous_description_is_unknown() -> None:
    assert verdict("Друзья", "Общаемся обо всём подряд")[0] == UNKNOWN


def test_market_marker_only_in_link_or_contact_is_weak_so_unknown() -> None:
    cls, evidence = verdict("Общение", "Пишите админу @rent_admin или t.me/bike_shop")
    assert cls == UNKNOWN
    assert "слабое доказательство" in evidence


def test_off_topic_marker_only_in_link_is_weak_so_unknown() -> None:
    cls, evidence = verdict("Общение", "Все вопросы в t.me/yoga_help или @medic_bot")
    assert cls == UNKNOWN
    assert "слабое доказательство" in evidence


def test_foreign_city_needs_explicit_city_in_description_and_no_nha_trang() -> None:
    cls, evidence = verdict("Барахолка", "Аренда жилья и байков в Ханое")
    assert cls == FOREIGN_CITY
    assert "хано" in evidence


def test_foreign_city_is_not_derived_from_the_title_alone() -> None:
    assert verdict("Барахолка Ханой", "Купля-продажа")[0] == RELEVANT
    assert verdict("Hanoi market")[0] == RELEVANT


def test_nha_trang_anywhere_cancels_foreign_city() -> None:
    cls, _ = verdict("Нячанг и Далат", "Аренда жилья в Далате и Нячанге")
    assert cls == RELEVANT


@pytest.mark.parametrize("status,expected", [(200, "ok"), (404, "unavailable")])
async def test_fetch_maps_status_without_retries(status: int, expected: str) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status, text=page("Барахолка", "Аренда"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await fetch_preview(client, "flea")
    assert snapshot.status == expected
    assert calls == ["https://t.me/flea"]


async def test_network_error_is_unknown_not_an_exception_and_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await fetch_preview(client, "flea")
    assert snapshot == PreviewSnapshot(status="unavailable", extra="ConnectError")
    assert classify_preview(snapshot).cls == UNKNOWN
    assert calls == 1
