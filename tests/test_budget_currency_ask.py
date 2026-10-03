"""Бюджет в евро или рублях: бот переспрашивает в донгах или долларах, а не молчит.

Курса для EUR и RUB нет, и фильтр по ним ничего не режет. Молча принять такой бюджет значило бы
показать человеку выдачу без потолка, пока он уверен, что потолок есть.
"""

from __future__ import annotations

import pytest

from sniffer.domain.dialogue import CURRENCY_ASK, blocking_question
from sniffer.domain.passport import Budget, Category, Currency, Passport
from sniffer.simulation.stubs import MemoryStore
from tests.thread_support import CLIENT, Replies, talk


def passport(currency: Currency | None) -> Passport:
    return Passport(
        category=Category.MOTORBIKE,
        city="nha_trang",
        budget=Budget(max=300, currency=currency),
        raw_query="скутер",
    )


@pytest.mark.parametrize("currency", [Currency.EUR, Currency.RUB])
def test_an_unpriceable_currency_is_asked_about_once(currency: Currency) -> None:
    question = blocking_question(passport(currency), [])
    assert question is not None and question.field == "budget.max"
    assert question.text == CURRENCY_ASK and "донгах или долларах" in question.text
    values = {option.value.split()[-1] for option in question.options}
    assert values == {"USD", "VND"}, "кнопки только донги и доллары"
    assert blocking_question(passport(currency), ["budget.max"]) is None, "второй раз не спросим"


@pytest.mark.parametrize("currency", [Currency.USD, Currency.VND, None])
def test_priceable_or_unnamed_currency_is_not_asked(currency: Currency | None) -> None:
    assert blocking_question(passport(currency), []) is None


async def test_the_conversation_asks_before_searching_a_euro_budget() -> None:
    store = MemoryStore()
    said = Replies()

    await talk(store).on_text(CLIENT, "ищу скутер в нячанге до 300 евро", said)

    assert said.texts == [CURRENCY_ASK]
    assert said.sent[0].question is not None


async def test_a_dollar_answer_replaces_the_budget_and_search_goes_on() -> None:
    store = MemoryStore()
    talker = talk(store)
    await talker.on_text(CLIENT, "ищу скутер в нячанге до 300 евро", Replies())
    said = Replies()

    await talker.on_answer(CLIENT, "budget", "400 USD", said)

    current = (await store.load(CLIENT)).passport
    assert current is not None
    assert current.passport.budget.currency is Currency.USD
    assert current.passport.budget.max == 400
    assert CURRENCY_ASK not in said.texts
