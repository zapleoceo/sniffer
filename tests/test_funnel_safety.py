"""Regression coverage for mandatory funnel and selected-request refinements."""

from __future__ import annotations

import pytest

from sniffer.bot.conversation import Conversation, Found
from sniffer.domain.dialogue import SKIP
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.search.intake_rules import parse_query
from tests.test_bot_dialog import (
    CLIENT,
    FakeJournal,
    MemoryStore,
    Replies,
    RulesIntake,
    bike,
)


async def test_word_skip_cannot_bypass_mandatory_city_or_category() -> None:
    store = MemoryStore()
    replies = Replies()
    calls: list[Passport] = []

    async def finder(_passport: Passport) -> Found:
        calls.append(_passport)
        return Found(items=[])

    talker = Conversation(store, intake=RulesIntake, finder=finder, recorder=FakeJournal())
    await talker.on_text(CLIENT, "нужен скутер", replies)
    assert replies.sent[-1].question is not None
    assert replies.sent[-1].question.field == "city"

    await talker.on_text(CLIENT, "не важно", replies)
    assert replies.sent[-1].question is not None
    assert replies.sent[-1].question.field == "city"

    await talker.on_answer(CLIENT, "city", "nha_trang", replies)
    await talker.on_text(CLIENT, "не важно", replies)
    # Budget is the only optional funnel field, so its skip proceeds to search.
    assert replies.sent[-1].question is None
    assert len(calls) == 1


async def test_callback_skip_cannot_bypass_mandatory_category_or_city() -> None:
    store = MemoryStore()
    replies = Replies()

    async def finder(_passport: Passport) -> Found:
        return Found(items=[])

    talker = Conversation(
        store,
        intake=RulesIntake,
        finder=finder,
        recorder=FakeJournal(),
    )

    await talker.on_text(CLIENT, "ищу что-нибудь", replies)
    assert replies.sent[-1].question is not None
    assert replies.sent[-1].question.field == "category"
    await talker.on_answer(CLIENT, "cat", SKIP, replies)
    assert replies.sent[-1].question is not None
    assert replies.sent[-1].question.field == "category"

    await talker.on_text(CLIENT, "скутер", replies)
    await talker.on_answer(CLIENT, "city", SKIP, replies)
    assert replies.sent[-1].question is not None
    assert replies.sent[-1].question.field == "city"


async def test_repeat_of_draft_still_runs_the_mandatory_funnel() -> None:
    store = MemoryStore()
    draft = await store.start(
        await store.load(CLIENT),
        bike(city=None, budget=Budget()),
    )
    assert draft.passport is not None
    calls: list[Passport] = []

    async def finder(passport: Passport) -> Found:
        calls.append(passport)
        return Found(items=[])

    talker = Conversation(store, intake=RulesIntake, finder=finder, recorder=FakeJournal())
    replies = Replies()
    await talker.repeat(CLIENT, draft.passport.root, replies)

    assert not calls
    assert replies.sent[-1].question is not None
    assert replies.sent[-1].question.field == "city"


async def test_scooter_category_button_sets_automatic_transmission() -> None:
    store = MemoryStore()
    replies = Replies()

    async def finder(_passport: Passport) -> Found:
        return Found(items=[])

    talker = Conversation(
        store,
        intake=RulesIntake,
        finder=finder,
        recorder=FakeJournal(),
    )

    await talker.on_text(CLIENT, "ищу что-нибудь", replies)
    await talker.on_answer(CLIENT, "cat", "scooter", replies)
    await talker.on_answer(CLIENT, "city", "nha_trang", replies)
    await talker.on_answer(CLIENT, "budget", "500 USD", replies)

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.passport.attributes["transmission"] == "automatic"
    assert current.passport.passport.attributes["body_type"] == "tay_ga"


async def test_apartment_category_button_defaults_to_rent() -> None:
    store = MemoryStore()
    replies = Replies()

    async def finder(_passport: Passport) -> Found:
        return Found(items=[])

    talker = Conversation(store, intake=RulesIntake, finder=finder, recorder=FakeJournal())
    await talker.on_text(CLIENT, "ищу жильё", replies)
    await talker.on_answer(CLIENT, "cat", "apartment", replies)
    await talker.on_answer(CLIENT, "city", "nha_trang", replies)
    await talker.on_answer(CLIENT, "budget", "500 USD", replies)

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.passport.intent is Intent.RENT


@pytest.mark.parametrize("editing", [False, True])
async def test_budget_refinement_preserves_selected_request(editing: bool) -> None:
    store = MemoryStore()
    first = await store.start(
        await store.load(CLIENT),
        parse_query("Yamaha Da Nang до 800 USD"),
    )
    assert first.passport is not None
    await store.select(first, first.passport.root, editing=editing)
    replies = Replies()

    async def finder(_passport: Passport) -> Found:
        return Found(items=[])

    talker = Conversation(
        store,
        intake=RulesIntake,
        finder=finder,
        recorder=FakeJournal(),
    )

    await talker.on_text(CLIENT, "до 500 USD", replies)

    current = await store.load(CLIENT)
    assert current.passport is not None
    revised = current.passport.passport
    assert current.passport.root == first.passport.root
    assert revised.category == first.passport.passport.category
    assert revised.city == first.passport.passport.city
    assert revised.attributes == first.passport.passport.attributes
    assert revised.budget.max == 500
    assert revised.budget.currency is Currency.USD


async def test_explicit_edit_of_draft_supplies_intent_from_new_text() -> None:
    store = MemoryStore()
    draft = await store.start(
        await store.load(CLIENT),
        Passport(category=None, city=None, raw_query="что-нибудь"),
    )
    assert draft.passport is not None
    await store.select(draft, draft.passport.root, editing=True)
    replies = Replies()

    async def finder(_passport: Passport) -> Found:
        return Found(items=[])

    talker = Conversation(store, intake=RulesIntake, finder=finder, recorder=FakeJournal())
    await talker.on_text(CLIENT, "куплю скутер в Нячанге до 500 USD", replies)

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.passport.intent is Intent.BUY
    assert current.passport.passport.category is Category.MOTORBIKE


async def test_explicit_edit_can_change_rent_to_buy() -> None:
    store = MemoryStore()
    first = await store.start(
        await store.load(CLIENT),
        parse_query("сниму квартиру в Нячанге до 500 USD"),
    )
    assert first.passport is not None
    await store.select(first, first.passport.root, editing=True)
    replies = Replies()

    async def finder(_passport: Passport) -> Found:
        return Found(items=[])

    talker = Conversation(store, intake=RulesIntake, finder=finder, recorder=FakeJournal())
    await talker.on_text(CLIENT, "куплю квартиру в Нячанге до 500 USD", replies)

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.passport.intent is Intent.BUY
    assert current.passport.passport.category is Category.APARTMENT


async def test_city_reply_with_budget_finishes_scooter_funnel() -> None:
    store = MemoryStore()
    replies = Replies()
    calls: list[Passport] = []

    async def finder(passport: Passport) -> Found:
        calls.append(passport)
        return Found(items=[])

    talker = Conversation(store, intake=RulesIntake, finder=finder, recorder=FakeJournal())
    await talker.on_text(CLIENT, "нужен скутер", replies)
    assert replies.sent[-1].question is not None
    assert replies.sent[-1].question.field == "city"
    await talker.on_text(CLIENT, "Дананг до 500", replies)

    assert not [
        reply for reply in replies.sent if reply.question and reply.question.field == "budget.max"
    ]
    assert len(calls) == 1
    assert calls[0].city == "da_nang"
    assert calls[0].budget.max == 500
    assert calls[0].attributes["transmission"] == "automatic"


async def test_explicit_brand_change_drops_old_model() -> None:
    store = MemoryStore()
    first = await store.start(
        await store.load(CLIENT),
        bike(attributes={"brand": "honda", "model": "lead", "transmission": "automatic"}),
    )
    assert first.passport is not None
    await store.select(first, first.passport.root, editing=True)
    replies = Replies()

    async def finder(_passport: Passport) -> Found:
        return Found(items=[])

    talker = Conversation(store, intake=RulesIntake, finder=finder, recorder=FakeJournal())
    await talker.on_text(CLIENT, "Yamaha", replies)

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.passport.attributes["brand"] == "yamaha"
    assert "model" not in current.passport.passport.attributes
