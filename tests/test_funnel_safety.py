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


async def test_word_skip_cannot_bypass_mandatory_category() -> None:
    """search-first (владелец, 04.09.2026): единственное обязательное поле —
    категория, без предмета нечего искать. Город больше не в воронке —
    `blocking_question` (`domain/dialogue.py`) его не спрашивает вовсе. Здесь
    проверяется, что «не важно» словами не пропускает категорию, а как только
    она названа, бот ищет сразу, не дожидаясь города.
    """
    store = MemoryStore()
    replies = Replies()
    calls: list[Passport] = []

    async def finder(_passport: Passport) -> Found:
        calls.append(_passport)
        return Found(items=[])

    talker = Conversation(store, intake=RulesIntake, finder=finder, recorder=FakeJournal())
    await talker.on_text(CLIENT, "ищу что-нибудь", replies)
    assert replies.sent[-1].question is not None
    assert replies.sent[-1].question.field == "category"

    await talker.on_text(CLIENT, "не важно", replies)
    assert replies.sent[-1].question is not None
    assert replies.sent[-1].question.field == "category", (
        "категорию нельзя пропустить словом «не важно»"
    )
    assert not calls

    await talker.on_text(CLIENT, "скутер", replies)
    assert replies.sent[-1].question is None, (
        "категория названа — дальше сразу выдача, без вопроса про город"
    )
    assert len(calls) == 1
    assert calls[0].city is None, (
        "RulesIntake без default_city — город остаётся тем, что было в тексте (ничем)"
    )


async def test_callback_skip_cannot_bypass_mandatory_category() -> None:
    """search-first: категорию не пропустить кнопкой «не важно». Города в
    этой воронке кнопкой больше не спрашивают вовсе — «нажатие» по коду
    "city", когда никакого вопроса не висит, `Conversation._answered` молча
    игнорирует, как и любую кнопку под уже неактуальной клавиатурой
    (сравни `test_stale_button_does_not_answer_twice` в test_bot_dialog.py).
    """
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
    assert replies.sent[-1].question.field == "category", "категорию нельзя пропустить кнопкой"

    await talker.on_text(CLIENT, "скутер", replies)
    assert replies.sent[-1].question is None, (
        "категория названа — сразу выдача, город не спрашивается"
    )

    after_search = len(replies.sent)
    await talker.on_answer(CLIENT, "city", SKIP, replies)
    assert len(replies.sent) == after_search, (
        "кнопки «город» под этим ответом уже нет — нажатие молча игнорируется"
    )


async def test_repeat_of_a_categoryless_draft_still_asks_for_it() -> None:
    """search-first (04.09.2026): единственное обязательное поле — категория.

    Раньше здесь проверялся пропуск ГОРОДА (`bike(city=None, ...)`): повтор
    черновика без города всё равно упирался в вопрос воронки. Город больше не
    в воронке и ничего не блокирует (см. тест ниже) — мандатной осталась
    только категория, и повтор черновика без неё обязан спросить, а не искать
    по пустому предмету.
    """
    store = MemoryStore()
    draft = await store.start(
        await store.load(CLIENT),
        bike(category=None, budget=Budget()),
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
    assert replies.sent[-1].question.field == "category"


async def test_repeat_of_a_cityless_draft_searches_without_asking() -> None:
    """Город больше не в воронке: черновик без города всё равно ищет сразу —
    категории достаточно, `blocking_question` про город не спрашивает вовсе.
    Дополняет тест выше: там нельзя пропустить категорию, здесь — можно смело
    не иметь города, и это не брак, а сама суть search-first.
    """
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

    assert len(calls) == 1
    assert calls[0].city is None, (
        "город не подставляется задним числом при повторе — он и не спрашивался"
    )
    assert replies.sent[-1].question is None


async def test_scooter_category_button_sets_automatic_transmission() -> None:
    """search-first: категория — единственный вопрос, поэтому ответ на него
    сразу уходит в поиск. Прежде здесь ещё отвечали на город и бюджет — эти
    вопросы больше не задаются, и такие ответы были бы кнопками в пустоту
    (`Conversation._answered` молча игнорирует нажатие без висящего вопроса).
    """
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
    assert replies.sent[-1].question is None, "категория названа — дальше сразу выдача"

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.passport.attributes["transmission"] == "automatic"
    assert current.passport.passport.attributes["body_type"] == "tay_ga"


async def test_apartment_category_button_defaults_to_rent() -> None:
    """search-first: см. комментарий у `test_scooter_category_button_sets_
    automatic_transmission` — город и бюджет здесь по той же причине не
    спрашиваются и не отвечаются.
    """
    store = MemoryStore()
    replies = Replies()

    async def finder(_passport: Passport) -> Found:
        return Found(items=[])

    talker = Conversation(store, intake=RulesIntake, finder=finder, recorder=FakeJournal())
    await talker.on_text(CLIENT, "ищу жильё", replies)
    await talker.on_answer(CLIENT, "cat", "apartment", replies)
    assert replies.sent[-1].question is None, "категория названа — дальше сразу выдача"

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


@pytest.mark.skip(
    reason=(
        "search-first (владелец, 04.09.2026) убрал вопрос про город и бюджет до "
        "выдачи. Этот тест проверял, что ОДНО свободное сообщение «Дананг до 500», "
        "отвечая на висящий вопрос про город, попутно называет и бюджет, закрывая "
        "воронку разом. Теперь после «нужен скутер» бот ищет сразу же (категория "
        "уже названа), ни один вопрос не висит — и «Дананг до 500» отвечать уже "
        "не на что: это не restates() (город меняется) и не corrects() (нет «не X, "
        "а Y»), значит это НОВЫЙ черновик, а он не называет категорию и бот вновь "
        "спрашивает «Что ищем?» вместо того, чтобы дополнить уже найденный запрос. "
        "Проверено прогоном (не теория): itemized в отчёте задачи 2026-09-04. Это "
        "устройство dialogue.py/conversation.py (не трогать по заданию), а не "
        "дефект — но и не то же самое поведение, что было. Затянуть тест до "
        "зелёного можно только соврав об ожиданиях или придумав новый механизм "
        "слияния свободного текста с уже показанной выдачей — решение продукта, "
        "не тестового агента, поэтому тест остановлен, а не переписан."
    )
)
async def test_city_reply_with_budget_finishes_scooter_funnel() -> None:
    """ОТКЛЮЧЕН (search-first, 04.09.2026) — причина в маркере `skip` выше.

    Раньше эта живая последовательность («нужен скутер» → вопрос про город →
    «Дананг до 500» одним сообщением закрывает и город, и бюджет) была тем, как
    заканчивалась воронка. Тело оставлено документацией прежнего поведения: если
    продукт решит поддержать склейку свободного текста с уже найденным запросом
    без промежуточного вопроса, тест можно включить обратно, поправив ожидания
    под новый механизм.
    """
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
