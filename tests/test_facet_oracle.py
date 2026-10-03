"""Кнопка сужения обещает ровно столько карточек, сколько оставит ответ.

Оракул — настоящий `rank_items`, а не второй предикат в тесте: отчёт фасетов
считал по атрибутам, а отсев читает текст, и «Vision 6» превращалось в ноль
после нажатия (ревью Opus волны 2, B3).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sniffer.domain.clarify import Ask, ClarificationPlanner, Search
from sniffer.domain.dialogue import DialogueState
from sniffer.domain.facets import facets_from
from sniffer.domain.fields import apply_answer, parse_option
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.search.facet_check import answer_survivor
from sniffer.search.relevance import rank_items
from sniffer.sources.base import RawItem

NOW = datetime.now(UTC)
RATE = 25_000.0


def item(n: int, title: str, attrs: dict[str, object]) -> RawItem:
    return RawItem(
        source="archive",
        external_id=str(n),
        url=f"https://t.me/c/1/{n}",
        title=title,
        price_vnd=20_000_000,
        posted_at=NOW,
        raw={"listing_id": n, "attributes": attrs, "category": "motorbike"},
    )


def market() -> list[RawItem]:
    items = [
        item(i, f"Honda Lead 2019 #{i}", {"brand": "honda", "model": "lead"}) for i in range(12)
    ]
    items += [
        item(100 + i, f"Yamaha Exciter #{i}", {"brand": "yamaha", "model": "exciter"})
        for i in range(10)
    ]
    # Модель известна только из фактов, в заголовке её нет: отсев по тексту такой лот не пропустит.
    items += [
        item(200 + i, f"Honda xe ga dep #{i}", {"brand": "honda", "model": "vision"})
        for i in range(6)
    ]
    items += [item(300 + i, f"Bán xe máy #{i}", {}) for i in range(8)]
    return items


def passport() -> Passport:
    return Passport(
        intent=Intent.BUY,
        category=Category.MOTORBIKE,
        city="nha_trang",
        budget=Budget(max=2000, currency=Currency.USD),
        raw_query="скутер",
    )


def test_every_button_count_is_what_the_real_ranking_leaves() -> None:
    pp = passport()
    found = rank_items(pp, market(), usd_vnd=RATE, now=NOW)
    report = facets_from(found, survives=answer_survivor(pp, RATE))
    step = ClarificationPlanner().decide(pp, report, DialogueState())
    assert isinstance(step, Ask)
    checked = 0
    for option in step.question.options:
        label_count = int(option.label.rsplit(" ", 1)[1])
        answered = apply_answer(
            pp, step.question.field, parse_option(step.question.field, option.value)
        )
        assert label_count == len(rank_items(answered, found, usd_vnd=RATE, now=NOW)), option.label
        checked += 1
    assert checked >= 2


def test_a_value_the_ranking_would_drop_is_not_offered() -> None:
    pp = passport()
    found = rank_items(pp, market(), usd_vnd=RATE, now=NOW)
    report = facets_from(found, survives=answer_survivor(pp, RATE))
    values = {entry.value for entry in report.facets["attributes.model"].values}
    assert "vision" not in values
    assert {"lead", "exciter"} <= values


def test_without_a_check_the_old_counting_stays() -> None:
    found = rank_items(passport(), market(), usd_vnd=RATE, now=NOW)
    values = {entry.value for entry in facets_from(found).facets["attributes.model"].values}
    assert "vision" in values


def test_a_capped_selection_says_not_less_than() -> None:
    pp = passport()
    found = rank_items(pp, market(), usd_vnd=RATE, now=NOW)
    step = ClarificationPlanner().decide(pp, facets_from(found, capped=True), DialogueState())
    assert isinstance(step, Ask)
    assert step.question.text.startswith("Подходит не меньше ")
    assert "не меньше" in step.question.buttons[-1].label
    plain = ClarificationPlanner().decide(pp, facets_from(found), DialogueState())
    assert isinstance(plain, Ask)
    assert "не меньше" not in plain.question.text
    assert isinstance(ClarificationPlanner().decide(pp, facets_from([]), DialogueState()), Search)
