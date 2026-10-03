"""Показ выдачи через квоту: остаток, допущенные карточки, честная строка, предложение.

Чистые функции: решение квоты (`Gate`) подставлено готовым, база и Telegram не нужны.
Что именно квота решает — в `test_quota_*`; здесь — как решение выглядит для человека.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sniffer.bot import wording, wording_plan
from sniffer.bot.presenter import Gate, Reply, present, present_offer
from sniffer.domain.dialogue import feedback_buttons
from sniffer.domain.passport import Category, Intent, Passport
from sniffer.domain.plans import FREE_CARDS_PER_PERIOD, PAID_CARDS_PER_PERIOD
from sniffer.domain.quota import Admission
from sniffer.sources.base import RawItem

ROOT = 41
END = datetime(2026, 11, 17, 2, 30, tzinfo=UTC)
FRESH = datetime.now(UTC) - timedelta(days=1)


@dataclass
class Result:
    items: list[RawItem] = field(default_factory=list)
    status: str | None = None
    deferred: bool = False
    capped: bool = False


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


def bike() -> Passport:
    return Passport(
        intent=Intent.BUY,
        category=Category.MOTORBIKE,
        city="nha_trang",
        attributes={"brand": "Honda"},
    )


def cards_in(reply: Reply) -> int:
    return reply.text.count("открыть оригинал")


def admission(
    *, granted: int = 0, withheld: int = 0, limit: int | None = 10, remaining: int | None = 0
) -> Admission:
    return Admission(
        granted=tuple(range(granted)),
        withheld=tuple(range(100, 100 + withheld)),
        remaining=remaining,
        limit=limit,
        period_end=END if limit is not None else None,
    )


def gate(found: list[RawItem], shown: int, *, offer: bool = False, **kwargs: int | None) -> Gate:
    return Gate(
        admission=admission(**kwargs),  # type: ignore[arg-type]
        shown=tuple(found[:shown]),
        offer=offer,
    )


# ── часть карточек пустили, часть удержал лимит ─────────────────────────────


def test_a_partial_issue_has_the_balance_the_header_of_what_is_shown_and_the_honest_rest() -> None:
    found, passport = items(37), bike()

    reply = present(passport, Result(found), root=ROOT, gate=gate(found, 4, granted=4, withheld=1))

    expected_head = (
        f"{wording_plan.balance_line(10, 0, END)}\n\n{wording.result_header(passport, 37, 4)}\n\n"
    )
    assert reply.text.startswith(expected_head)
    assert cards_in(reply) == 4
    assert reply.text.endswith(wording_plan.more_line(33, limit=10, renews=END))
    assert reply.feedback == feedback_buttons(passport)
    assert reply.offer_subscription is True and reply.passport_root == ROOT


def test_the_header_and_the_cards_count_the_same_number_when_the_limit_cut_the_page() -> None:
    """«Показываю 2» над двумя карточками, а не над пятью — число одно на заголовок и срез."""
    found = items(10)

    reply = present(bike(), Result(found), root=ROOT, gate=gate(found, 2, granted=2, withheld=3))

    assert cards_in(reply) == 2
    assert "показываю 2 лучших" in reply.text


def test_the_cards_are_exactly_the_admitted_ones_not_just_the_first_few() -> None:
    """Допущены не обязательно первые: виденные раньше и опознанные идут вперемешку."""
    found = items(6)
    picked = Gate(admission=admission(granted=2, remaining=8), shown=(found[1], found[4]))

    reply = present(bike(), Result(found), root=ROOT, gate=picked)

    assert "Honda Vision 1" in reply.text and "Honda Vision 4" in reply.text
    assert "Honda Vision 0" not in reply.text and "Honda Vision 2" not in reply.text
    assert cards_in(reply) == 2


def test_nothing_is_held_back_means_no_line_about_the_rest() -> None:
    found = items(3)

    reply = present(bike(), Result(found), root=ROOT, gate=gate(found, 3, granted=3, remaining=7))

    assert "Бесплатно осталось 7 из 10 до 17 ноября." in reply.text
    assert "Ещё" not in reply.text


def test_a_status_line_stands_above_the_balance() -> None:
    found = items(2)

    reply = present(
        bike(),
        Result(found, status="Каталог ещё обновляется."),
        root=ROOT,
        gate=gate(found, 2, granted=2, remaining=8),
    )

    assert reply.text.startswith("Каталог ещё обновляется.\n\nБесплатно осталось 8 из 10")


def test_the_owner_sees_neither_a_balance_nor_a_line_about_the_rest() -> None:
    found = items(5)

    reply = present(
        bike(), Result(found), root=ROOT, gate=gate(found, 5, granted=5, limit=None, remaining=None)
    )

    assert "осталось" not in reply.text.lower() and "Ещё" not in reply.text
    assert cards_in(reply) == 5


def test_a_subscriber_at_the_ceiling_is_told_about_the_renewal_not_sold_a_plan() -> None:
    found = items(10)

    reply = present(
        bike(),
        Result(found),
        root=ROOT,
        gate=gate(found, 2, granted=2, withheld=3, limit=PAID_CARDS_PER_PERIOD),
    )

    assert "после обновления лимита 17 ноября" in reply.text
    assert "по подписке" not in reply.text


# ── показать нечего: само сообщение — предложение или короткий ответ ────────


def test_when_nothing_may_be_shown_the_message_is_the_offer_with_the_plan_button() -> None:
    found = items(37)

    reply = present(bike(), Result(found), root=ROOT, gate=gate(found, 0, withheld=5, offer=True))

    assert reply.text == wording_plan.exhausted_offer(total=37, renews=END)
    assert reply.offer_plan is True
    assert reply.feedback == () and reply.offer_subscription is False
    assert reply.passport_root == ROOT and cards_in(reply) == 0


def test_the_second_time_the_same_day_it_is_a_short_answer_without_a_payment_button() -> None:
    found = items(37)

    reply = present(bike(), Result(found), root=ROOT, gate=gate(found, 0, withheld=5, offer=False))

    assert reply.text == wording_plan.exhausted_short(total=37, renews=END)
    assert reply.offer_plan is False


def test_a_subscriber_at_the_ceiling_gets_information_even_when_the_offer_was_free() -> None:
    found = items(37)

    reply = present(
        bike(),
        Result(found),
        root=ROOT,
        gate=gate(found, 0, withheld=5, offer=True, limit=PAID_CARDS_PER_PERIOD),
    )

    assert reply.text == wording_plan.exhausted_cap(total=37, renews=END)
    assert reply.offer_plan is False


# ── отдельное предложение после частичной выдачи ────────────────────────────


def test_after_a_partial_issue_the_offer_is_one_separate_message_with_the_plan_button() -> None:
    found = items(10)
    partial = gate(found, 2, granted=2, withheld=3, offer=True)

    extra = present_offer(partial, root=ROOT)

    assert extra is not None
    assert extra.text == wording_plan.exhausted_offer(total=None, renews=END)
    assert extra.offer_plan is True and extra.passport_root == ROOT


def test_the_separate_offer_exists_only_when_every_condition_holds() -> None:
    found = items(10)

    assert present_offer(gate(found, 2, granted=2, withheld=3, offer=False), root=ROOT) is None
    assert present_offer(gate(found, 2, granted=2, withheld=0, offer=True), root=ROOT) is None
    assert present_offer(gate(found, 0, withheld=3, offer=True), root=ROOT) is None, (
        "когда показать нечего, предложение — само основное сообщение, второго нет"
    )
    capped = gate(found, 2, granted=2, withheld=3, offer=True, limit=PAID_CARDS_PER_PERIOD)
    assert present_offer(capped, root=ROOT) is None, "подписчику на потолке подписку не продают"


def test_the_free_ceiling_in_the_texts_is_the_plans_constant() -> None:
    found = items(3)

    reply = present(bike(), Result(found), root=ROOT, gate=gate(found, 0, withheld=3, offer=True))

    assert f"Бесплатные {FREE_CARDS_PER_PERIOD} карточек закончились" in reply.text


def test_without_a_gate_the_presenter_still_works_as_before() -> None:
    """Контракт этапа 1: без квоты выдача та же, что была до неё."""
    found = items(3)

    reply = present(bike(), Result(found), root=ROOT)

    assert "осталось" not in reply.text.lower() and cards_in(reply) == 3
