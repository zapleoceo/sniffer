"""Показ выдачи: что уходит клиенту, когда поиск закончился.

Чистые функции, без базы и Telegram. Поведение перенесено из
`Conversation._search` без изменений, и перенос доказан сравнением со старым
модулем на десятках тысяч комбинаций; здесь закреплено то, что должно
остаться верным после следующих правок: заголовок и карточки считают одно и то
же число, служебный статус стоит над заголовком, пустой ответ предлагает
слежение, а ответ «жду сбора» — нет.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from sniffer.bot import wording
from sniffer.bot.billing import OFFER
from sniffer.bot.presenter import Reply, present
from sniffer.config import reload_settings
from sniffer.domain.dialogue import feedback_buttons
from sniffer.domain.passport import Category, Intent, Passport
from sniffer.sources.base import RawItem

ROOT = 41
FRESH = datetime.now(UTC) - timedelta(days=1)


@dataclass
class Result:
    """Итог поиска в том виде, в каком его читает показ."""

    items: list[RawItem] = field(default_factory=list)
    status: str | None = None
    deferred: bool = False


def items(count: int) -> list[RawItem]:
    return [
        RawItem(
            source="archive",
            external_id=str(index),
            url=f"https://t.me/c/1/{index}",
            title=f"Honda Vision {index}",
            price_raw="25.000.000 đ",
            posted_at=FRESH,
        )
        for index in range(count)
    ]


def bike(**attributes: object) -> Passport:
    return Passport(
        intent=Intent.BUY, category=Category.MOTORBIKE, city="nha_trang", attributes=attributes
    )


def cards_in(reply: Reply) -> int:
    return reply.text.count("открыть оригинал")


def test_results_carry_header_cards_buttons_and_the_branch_root() -> None:
    passport = bike(brand="Honda")

    reply = present(passport, Result(items(3)), root=ROOT)

    assert reply.text.startswith(wording.result_header(passport, 3, 3))
    assert cards_in(reply) == 3
    assert reply.feedback == feedback_buttons(passport)
    assert reply.offer_subscription is True
    assert reply.passport_root == ROOT
    assert reply.question is None


def test_the_header_and_the_cards_count_one_and_the_same_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«Показываю 2» над тремя карточками — ровно тот сбой, ради которого число одно."""
    passport = bike(brand="Honda")
    for cap in (1, 2, 4):
        monkeypatch.setenv("MAX_CARDS", str(cap))
        reload_settings()
        try:
            reply = present(passport, Result(items(10)), root=ROOT)
        finally:
            monkeypatch.undo()
            reload_settings()

        assert cards_in(reply) == cap
        assert reply.text.startswith(wording.result_header(passport, 10, cap))


def test_a_status_line_stands_above_the_header() -> None:
    passport = bike(brand="Honda")

    reply = present(passport, Result(items(2), status="Каталог ещё обновляется."), root=ROOT)

    assert reply.text.startswith(
        "Каталог ещё обновляется.\n\n" + wording.result_header(passport, 2, 2)
    )


def test_an_empty_answer_names_what_is_missing_and_offers_to_follow() -> None:
    passport = bike(brand="Honda")

    reply = present(passport, Result(), root=ROOT)

    assert reply.text == f"{wording.nothing_found(passport)}\n\n{OFFER}"
    assert reply.offer_subscription is True
    assert reply.passport_root == ROOT
    assert reply.feedback == ()


def test_an_empty_answer_with_a_status_says_the_status_instead_of_the_generic_line() -> None:
    reply = present(bike(), Result(status="Источники ответили не все."), root=ROOT)

    assert reply.text == f"Источники ответили не все.\n\n{OFFER}"
    assert wording.NOTHING_FOUND not in reply.text


def test_the_empty_line_depends_on_the_category() -> None:
    flat = Passport(intent=Intent.RENT, category=Category.APARTMENT, city="nha_trang")

    assert present(flat, Result(), root=ROOT).text.startswith(wording.nothing_found(flat))
    assert wording.nothing_found(flat) != wording.nothing_found(bike())


def test_a_deferred_answer_does_not_sell_anything_and_carries_no_buttons() -> None:
    """Сбор поставлен в очередь: человек ещё не увидел, что «искать больше негде»."""
    reply = present(bike(), Result(status="Собираю свежие объявления.", deferred=True), root=ROOT)

    assert reply == Reply("Собираю свежие объявления.")


def test_a_deferred_answer_without_a_status_falls_back_to_the_failure_line() -> None:
    assert present(bike(), Result(deferred=True), root=ROOT).text == wording.SEARCH_FAILED


def test_found_items_win_over_the_deferred_flag() -> None:
    """Флаг читается только у пустого ответа: карточки не прячутся за «жду»."""
    reply = present(bike(), Result(items(2), deferred=True), root=ROOT)

    assert cards_in(reply) == 2
    assert reply.offer_subscription is True


def test_a_reply_without_a_branch_stays_a_reply() -> None:
    assert present(bike(), Result(items(1)), root=None).passport_root is None
