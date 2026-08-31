"""Диалог целиком: текст клиента → уточнение → паспорт → план → карточки.

Telegram, сеть и модель заменены заглушками: проверяется поведение бота, а не
работа Bot API. Хранилище тоже подделка, но версии паспорта оно считает
по-настоящему — иначе тест про «новая версия, а не перезапись» проверял бы
подделку.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from aiogram.types import Message

from sniffer.bot import app as bot_app
from sniffer.bot.conversation import (
    NO_REQUEST_YET,
    NOTHING_FOUND,
    SEARCH_FAILED,
    Conversation,
    Reply,
)
from sniffer.bot.handlers import search as handler
from sniffer.bot.keyboards import AnswerCallback, FeedbackCallback, markup
from sniffer.bot.store import Client, Dialogue
from sniffer.domain.dialogue import (
    EVENT_USER_MESSAGE,
    SKIP,
    DialogueState,
    Feedback,
    advance,
    replay,
)
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.domain.records import PassportEvent, StoredPassport
from sniffer.sources.base import RawItem

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
CLIENT = Client(tg_user_id=42, username="dima")


class MemoryStore:
    """`DialogueStore` на словарях: диалог без Postgres, но с версиями."""

    def __init__(self) -> None:
        self.rows: list[StoredPassport] = []
        self.events: list[PassportEvent] = []
        self._users: dict[int, int] = {}

    async def load(self, client: Client) -> Dialogue:
        user_id = self._users.setdefault(client.tg_user_id, len(self._users) + 1)
        current = next(
            (row for row in reversed(self.rows) if row.user_id == user_id and row.is_current), None
        )
        if current is None:
            return Dialogue(user_id=user_id)
        root = current.root
        chain = {row.id for row in self.rows if row.id == root or row.root_id == root}
        events = [event for event in self.events if event.passport_id in chain]
        return Dialogue(user_id=user_id, passport=current, state=replay(events))

    async def start(self, dialogue: Dialogue, passport: Passport) -> Dialogue:
        stored = StoredPassport(
            id=len(self.rows) + 1, user_id=dialogue.user_id, version=1, passport=passport
        )
        self.rows.append(stored)
        self._event(stored.id, EVENT_USER_MESSAGE, {"text": passport.raw_query})
        return Dialogue(user_id=dialogue.user_id, passport=stored, state=DialogueState())

    async def revise(
        self, dialogue: Dialogue, passport: Passport, *, kind: str, payload: dict[str, Any]
    ) -> Dialogue:
        assert dialogue.passport is not None
        root = dialogue.passport.root
        self.rows = [
            replace(row, is_current=False) if row.id == root or row.root_id == root else row
            for row in self.rows
        ]
        stored = StoredPassport(
            id=len(self.rows) + 1,
            user_id=dialogue.user_id,
            version=dialogue.passport.version + 1,
            root_id=root,
            passport=passport,
        )
        self.rows.append(stored)
        self._event(stored.id, kind, payload)
        return Dialogue(
            user_id=dialogue.user_id, passport=stored, state=advance(dialogue.state, kind, payload)
        )

    async def note(self, dialogue: Dialogue, *, kind: str, payload: dict[str, Any]) -> Dialogue:
        assert dialogue.passport is not None
        self._event(dialogue.passport.id, kind, payload)
        return replace(dialogue, state=advance(dialogue.state, kind, payload))

    def _event(self, passport_id: int, kind: str, payload: dict[str, Any]) -> None:
        self.events.append(PassportEvent(passport_id=passport_id, kind=kind, payload=payload))


class FakeIntake:
    """Разбор без модели: отдаёт заранее заданный паспорт с текстом клиента."""

    def __init__(self, passport: Passport) -> None:
        self._passport = passport
        self.parsed: list[str] = []

    async def parse(self, text: str) -> Passport:
        self.parsed.append(text)
        return self._passport.model_copy(update={"raw_query": text})


class Replies:
    def __init__(self) -> None:
        self.sent: list[Reply] = []

    async def __call__(self, reply: Reply) -> None:
        self.sent.append(reply)

    @property
    def texts(self) -> list[str]:
        return [reply.text for reply in self.sent]


class FakeChat:
    id = 42


class FakeUser:
    def __init__(self, user_id: int = 42, username: str = "dima") -> None:
        self.id = user_id
        self.username = username


class FakeMessage:
    """Ровно то, что хендлер трогает у сообщения."""

    def __init__(self, text: str, *, from_user: FakeUser | None = None) -> None:
        self.text = text
        self.chat = FakeChat()
        self.from_user = from_user
        self.answers: list[tuple[str, Any]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs.get("reply_markup")))


def found(external_id: str, *, age_days: int = 1) -> RawItem:
    return RawItem(
        source="chotot",
        external_id=external_id,
        url=f"https://www.chotot.com/{external_id}.htm",
        title=f"Honda Vision {external_id}",
        price_raw="25.000.000 đ",
        posted_at=NOW - timedelta(days=age_days),
    )


def bike(**overrides: object) -> Passport:
    fields: dict[str, object] = {
        "intent": Intent.BUY,
        "category": Category.MOTORBIKE,
        "city": "nha_trang",
        "raw_query": "ищу скутер в Нячанге",
    }
    fields.update(overrides)
    return Passport(**fields)  # type: ignore[arg-type]


def filled() -> Passport:
    return bike(
        budget=Budget(max=400, currency=Currency.USD),
        attributes={"transmission": "automatic", "condition": "good", "brand": "honda"},
    )


async def nothing(_passport: Passport) -> list[RawItem]:
    return []


def talk(
    store: MemoryStore,
    passport: Passport,
    *,
    items: list[RawItem] | None = None,
    finder: Any = None,
) -> Conversation:
    async def find(_passport: Passport) -> list[RawItem]:
        return items if items is not None else [found("1")]

    return Conversation(store, intake=lambda: FakeIntake(passport), finder=finder or find)


# ── выдача без вопросов ─────────────────────────────────────────────────────


async def test_full_passport_goes_straight_to_search() -> None:
    """Спрашивать нечего — значит, и не спрашиваем."""
    replies = Replies()
    await talk(MemoryStore(), filled()).on_text(CLIENT, "ищу скутер", replies)

    assert len(replies.sent) == 2
    assert "Ищу" in replies.texts[0]
    assert "открыть оригинал" in replies.texts[1]
    assert replies.sent[1].question is None


async def test_cards_carry_the_feedback_buttons() -> None:
    """Показанная выдача уточняет запрос лучше вопроса — если есть чем ответить."""
    replies = Replies()
    await talk(MemoryStore(), filled()).on_text(CLIENT, "ищу скутер", replies)

    kinds = {option.value for option in replies.sent[-1].feedback}
    assert {Feedback.PRICEY.value, Feedback.WRONG.value} <= kinds


async def test_nothing_found_is_said_out_loud() -> None:
    replies = Replies()
    await talk(MemoryStore(), filled(), items=[]).on_text(CLIENT, "ищу вертолёт", replies)

    assert replies.texts[-1] == NOTHING_FOUND


async def test_broken_search_still_answers() -> None:
    """Клиент не должен остаться без ответа из-за чужого сломанного API."""

    async def boom(_passport: Passport) -> list[RawItem]:
        raise RuntimeError("источник отдал не то")

    replies = Replies()
    await talk(MemoryStore(), filled(), finder=boom).on_text(CLIENT, "ищу скутер", replies)

    assert replies.texts[-1] == SEARCH_FAILED


async def test_old_lot_is_marked_in_the_answer() -> None:
    """Требование verifier'а доезжает до клиента, а не остаётся в коде."""
    replies = Replies()
    await talk(MemoryStore(), filled(), items=[found("old", age_days=40)]).on_text(
        CLIENT, "ищу скутер", replies
    )

    assert "могло быть продано" in replies.texts[-1]


async def test_empty_message_is_ignored() -> None:
    replies = Replies()
    await talk(MemoryStore(), filled()).on_text(CLIENT, "   ", replies)

    assert replies.sent == []


# ── уточняющие вопросы ──────────────────────────────────────────────────────


async def test_missing_budget_is_asked_before_searching() -> None:
    """Ровно то, чего не хватало владельцу: сначала уточнение, потом поиск."""
    searched: list[Passport] = []

    async def find(passport: Passport) -> list[RawItem]:
        searched.append(passport)
        return []

    replies = Replies()
    await talk(MemoryStore(), bike(), finder=find).on_text(CLIENT, "ищу скутер", replies)

    assert len(replies.sent) == 1
    assert replies.sent[0].question is not None
    assert replies.sent[0].question.field == "budget.max"
    assert searched == [], "поиск не стартует, пока висит вопрос"


async def test_every_question_offers_a_way_out() -> None:
    replies = Replies()
    await talk(MemoryStore(), bike()).on_text(CLIENT, "ищу скутер", replies)

    question = replies.sent[0].question
    assert question is not None
    assert [option.value for option in question.buttons][-1] == SKIP


async def test_two_gaps_are_asked_one_by_one_and_never_more_than_three() -> None:
    """Максимум три вопроса за диалог, дальше — выдача по тому, что есть."""
    store = MemoryStore()
    talker = talk(store, bike())

    replies = Replies()
    await talker.on_text(CLIENT, "ищу скутер", replies)
    assert replies.sent[0].question is not None
    assert replies.sent[0].question.field == "budget.max"

    second = Replies()
    await talker.on_answer(CLIENT, "budget", "500", second)
    assert second.sent[0].question is not None
    assert second.sent[0].question.field == "attributes.transmission"

    third = Replies()
    await talker.on_answer(CLIENT, "trans", "automatic", third)
    assert third.sent[0].question is not None
    assert third.sent[0].question.field == "attributes.condition"

    fourth = Replies()
    await talker.on_answer(CLIENT, "cond", "good", fourth)
    assert [reply.question for reply in fourth.sent] == [None, None], "четвёртого вопроса нет"
    assert "Ищу" in fourth.texts[0]

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.passport.budget.max == 500
    assert current.passport.passport.attributes["transmission"] == "automatic"


async def test_skip_button_sends_us_searching() -> None:
    """«Не важно» — не ответ, а разрешение не спрашивать больше."""
    store = MemoryStore()
    talker = talk(store, bike())
    await talker.on_text(CLIENT, "ищу скутер", Replies())

    replies = Replies()
    await talker.on_answer(CLIENT, "budget", SKIP, replies)

    assert "Ищу" not in replies.texts[0], "после пропуска идёт следующий вопрос"
    assert replies.sent[0].question is not None

    versions = [row.version for row in store.rows]
    assert versions == [1], "пропуск ничего не меняет — значит, и версии не создаёт"


async def test_words_instead_of_a_button_are_understood() -> None:
    store = MemoryStore()
    talker = talk(store, bike())
    await talker.on_text(CLIENT, "ищу скутер", Replies())

    replies = Replies()
    await talker.on_text(CLIENT, "до 400 долларов", replies)

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.passport.budget.max == 400
    assert current.passport.passport.budget.currency is Currency.USD
    assert replies.sent[0].question is not None, "дальше идёт следующий вопрос"


async def test_skip_in_words_works_like_the_button() -> None:
    store = MemoryStore()
    talker = talk(store, bike())
    await talker.on_text(CLIENT, "ищу скутер", Replies())

    replies = Replies()
    await talker.on_text(CLIENT, "да не важно", replies)

    state = (await store.load(CLIENT)).state
    assert state.asked == ("budget.max", "attributes.transmission")
    assert [row.version for row in store.rows] == [1], "пропуск версии не создаёт"
    assert replies.sent[0].question is not None


async def test_text_that_is_not_an_answer_starts_a_new_request() -> None:
    """На вопрос о бюджете клиент отвечает новым запросом — это запрос, а не сумма.

    Заодно проверяется, что счётчик вопросов у новой цепочки свой: прежние три
    вопроса исчерпаны, и продолжись счёт — бот молча ушёл бы искать.
    """
    store = MemoryStore()
    talker = talk(store, bike())
    await talker.on_text(CLIENT, "ищу скутер", Replies())
    await talker.on_answer(CLIENT, "budget", SKIP, Replies())
    await talker.on_answer(CLIENT, "trans", SKIP, Replies())
    await talker.on_answer(CLIENT, "cond", SKIP, Replies())

    replies = Replies()
    await talker.on_text(CLIENT, "ладно, тогда квартиру в Нячанге", replies)

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.version == 1, "новая формулировка — новая цепочка, а не версия"
    assert current.passport.passport.raw_query == "ладно, тогда квартиру в Нячанге"
    assert current.state.asked == ("budget.max",), "счётчик вопросов начался заново"
    assert replies.sent[0].question is not None


async def test_stale_button_does_not_answer_twice() -> None:
    """Клавиатура остаётся в чате: второе нажатие не должно плодить версии."""
    store = MemoryStore()
    talker = talk(store, bike())
    await talker.on_text(CLIENT, "ищу скутер", Replies())
    await talker.on_answer(CLIENT, "budget", "500", Replies())

    replies = Replies()
    await talker.on_answer(CLIENT, "budget", "300", replies)

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.passport.budget.max == 500
    assert replies.sent == []


# ── обратная связь на карточках ─────────────────────────────────────────────


async def test_pricey_makes_a_new_version_and_searches_again() -> None:
    """Правка поля создаёт новую версию, а не переписывает старую (passport.md)."""
    store = MemoryStore()
    talker = talk(store, filled())
    await talker.on_text(CLIENT, "ищу скутер", Replies())

    replies = Replies()
    await talker.on_feedback(CLIENT, Feedback.PRICEY, replies)

    assert [row.version for row in store.rows] == [1, 2]
    assert store.rows[0].passport.budget.max == 400, "прежняя версия осталась как была"
    assert store.rows[0].is_current is False
    assert store.rows[1].passport.budget.max == 280
    assert store.rows[1].root == store.rows[0].id
    assert "Ищу" in replies.texts[0], "после уточнения подбор перезапускается"
    assert "открыть оригинал" in replies.texts[1]


async def test_automatic_feedback_fixes_the_transmission() -> None:
    """На «скутер» приехал спортбайк с механикой — кнопка чинит именно это."""
    store = MemoryStore()
    talker = talk(store, bike(budget=Budget(max=400, currency=Currency.USD)))
    await talker.on_text(CLIENT, "ищу скутер", Replies())
    await talker.on_answer(CLIENT, "trans", SKIP, Replies())

    await talker.on_feedback(CLIENT, Feedback.AUTOMATIC, Replies())

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.passport.attributes["transmission"] == "automatic"
    assert current.passport.version == 2


async def test_pricey_without_a_budget_asks_instead_of_guessing() -> None:
    store = MemoryStore()
    talker = talk(store, bike())
    await talker.on_text(CLIENT, "ищу скутер", Replies())
    await talker.on_answer(CLIENT, "budget", SKIP, Replies())
    await talker.on_answer(CLIENT, "trans", SKIP, Replies())
    await talker.on_answer(CLIENT, "cond", SKIP, Replies())

    replies = Replies()
    await talker.on_feedback(CLIENT, Feedback.PRICEY, replies)

    assert replies.sent[0].question is not None
    assert replies.sent[0].question.field == "budget.max"
    assert [row.version for row in store.rows] == [1], "спросить — не значит уже уточнить"


async def test_feedback_without_a_request_says_so() -> None:
    replies = Replies()
    await talk(MemoryStore(), filled()).on_feedback(CLIENT, Feedback.PRICEY, replies)

    assert replies.texts == [NO_REQUEST_YET]


# ── перезапуск процесса ─────────────────────────────────────────────────────


async def test_dialogue_survives_a_restart() -> None:
    """Бот перезапускается на каждом деплое — начатый разговор это переживает.

    Второй `Conversation` — это и есть перезапуск: у него нет ни памяти
    первого, ни его объектов. Всё, что осталось, — записи в хранилище.
    """
    store = MemoryStore()
    before = talk(store, bike())
    await before.on_text(CLIENT, "ищу скутер", Replies())

    after = talk(store, bike())
    replies = Replies()
    await after.on_text(CLIENT, "до 400", replies)

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.passport.budget.max == 400, "ответ лёг в начатый диалог"
    assert current.state.asked[0] == "budget.max"
    assert replies.sent[0].question is not None
    assert replies.sent[0].question.field == "attributes.transmission", "допрос не начался заново"


# ── проводка бота ───────────────────────────────────────────────────────────


async def test_start_explains_what_the_bot_does() -> None:
    hello = FakeMessage("/start")

    await handler.start(cast(Message, hello))

    assert len(hello.answers) == 1
    assert "объявлени" in hello.answers[0][0]


async def test_handler_draws_the_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Хендлер тонкий: берёт ответ разговора и рисует его кнопками."""
    talker = talk(MemoryStore(), bike())
    monkeypatch.setattr(handler, "conversation", lambda: talker)
    anonymous = FakeMessage("ищу скутер")

    await handler.search(cast(Message, anonymous))
    assert anonymous.answers == [], "пост от имени канала паспорту не принадлежит"

    request = FakeMessage("ищу скутер", from_user=FakeUser())
    await handler.search(cast(Message, request))

    text, keyboard = request.answers[0]
    assert "бюджет" in text.lower()
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "не важно, показать что есть" in labels


def test_answer_and_feedback_fit_the_callback_limit() -> None:
    """В callback_data влезает 64 байта — потому по проводу едут короткие ключи."""
    packed = AnswerCallback(code="trans", value="automatic").pack()
    feedback = FeedbackCallback(kind=Feedback.PRICEY.value).pack()

    assert len(packed.encode()) <= 64
    assert len(feedback.encode()) <= 64
    assert AnswerCallback.unpack(packed).value == "automatic"


def test_reply_without_buttons_has_no_keyboard() -> None:
    assert markup(Reply("просто текст")) is None


def test_dispatcher_knows_the_dialog() -> None:
    dispatcher = bot_app.build_dispatcher()

    assert [router.name for router in dispatcher.sub_routers] == ["search"]
