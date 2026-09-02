"""Диалог целиком: текст клиента → уточнение → паспорт → план → карточки.

Telegram, сеть и модель заменены заглушками: проверяется поведение бота, а не
работа Bot API. Хранилище тоже подделка, но версии паспорта оно считает
по-настоящему — иначе тест про «новая версия, а не перезапись» проверял бы
подделку.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from aiogram.types import Message

from sniffer.bot import app as bot_app
from sniffer.bot import journal
from sniffer.bot.conversation import (
    NO_REQUEST_YET,
    NOTHING_FOUND,
    NOTHING_TO_REFINE,
    SEARCH_FAILED,
    Conversation,
    Found,
    Reply,
)
from sniffer.bot.handlers import search as handler
from sniffer.bot.keyboards import AnswerCallback, FeedbackCallback, markup
from sniffer.bot.store import Client, Dialogue
from sniffer.broker import usage
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
from sniffer.search.intake_rules import parse_query
from sniffer.search.vocabulary import served_cities
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


class RulesIntake:
    """Разбор по правилам — как в бою без модели: город берётся из текста.

    `FakeIntake` для города не годится принципиально: он отдаёт заранее
    заданный паспорт, то есть подменяет ровно то поле, которое проверяется.
    """

    async def parse(self, text: str) -> Passport:
        return parse_query(text, default_city="nha_trang")


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


def vague() -> Passport:
    """Паспорт без категории: искать не на чём, пока не узнаем — что именно.

    Категория — единственное поле, без которого нельзя собрать план поиска
    (`blocking_question`). Поэтому теперь именно на вопросе «что ищем?» висит вся
    механика пропуска, ответа словами и повтора, раньше жившая на вопросе о
    бюджете. Всё прочее (бюджет, коробка, состояние) уточняется уже обратной
    связью на карточках, а не анкетой до выдачи.
    """
    return bike(category=None, raw_query="honda до 300")


async def nothing(_passport: Passport) -> Found:
    return Found(items=[])


def talk(
    store: MemoryStore,
    passport: Passport,
    *,
    items: list[RawItem] | None = None,
    finder: Any = None,
    recorder: Any = None,
) -> Conversation:
    async def find(_passport: Passport) -> Found:
        return Found(items=items if items is not None else [found("1")])

    # Журнал подставляется ВСЕГДА, даже когда тест о нём не спрашивает: без
    # подстановки разговор тянет соединение к настоящему Postgres, и тест либо
    # ждёт таймаут, либо зависит от того, поднята ли база рядом.
    return Conversation(
        store,
        intake=lambda: FakeIntake(passport),
        finder=finder or find,
        recorder=recorder or FakeJournal(),
    )


# ── выдача без вопросов ─────────────────────────────────────────────────────


async def test_full_passport_goes_straight_to_search() -> None:
    """Спрашивать нечего — значит, и не спрашиваем."""
    replies = Replies()
    await talk(MemoryStore(), filled()).on_text(CLIENT, "ищу скутер", replies)

    assert len(replies.sent) == 2
    assert "Ищу" in replies.texts[0]
    assert "открыть оригинал" in replies.texts[1]
    assert replies.sent[1].question is None


async def test_a_scooter_request_searches_without_a_form() -> None:
    """Жалоба владельца дословно: «нужен скутер» — это выдача, а не анкета.

    Категория известна, значит план поиска собрать есть из чего. Ни бюджет, ни
    коробку, ни состояние бот до первой выдачи не спрашивает — эти уточнения
    переехали на кнопки под карточками (passport.md: показать рано, уточнять
    обратной связью). Разбор настоящий (`RulesIntake`), чтобы тест ловил именно
    поведение бота на живой фразе, а не подставленный паспорт.
    """

    async def found_one(_passport: Passport) -> Found:
        return Found(items=[found("1")])

    store = MemoryStore()
    talker = Conversation(store, intake=RulesIntake, finder=found_one, recorder=FakeJournal())

    replies = Replies()
    await talker.on_text(CLIENT, "нужен скутер honda lead", replies)

    assert "Понял" in replies.texts[0] and "Ищу" in replies.texts[0]
    assert "открыть оригинал" in replies.texts[1], "карточки пришли сразу, без анкеты"
    # До выдачи не задано ни одного вопроса — ни про коробку, ни про бюджет, ни про состояние.
    assert [reply.question for reply in replies.sent] == [None, None]
    asked = {reply.question.field for reply in replies.sent if reply.question is not None}
    assert not asked & {"attributes.transmission", "budget.max", "attributes.condition"}


async def test_cards_carry_the_feedback_buttons() -> None:
    """Показанная выдача уточняет запрос лучше вопроса — если есть чем ответить."""
    replies = Replies()
    await talk(MemoryStore(), filled()).on_text(CLIENT, "ищу скутер", replies)

    kinds = {option.value for option in replies.sent[-1].feedback}
    assert {Feedback.PRICEY.value, Feedback.WRONG.value} <= kinds


async def test_nothing_found_is_said_out_loud() -> None:
    replies = Replies()
    await talk(MemoryStore(), filled(), items=[]).on_text(CLIENT, "ищу вертолёт", replies)

    assert NOTHING_FOUND in replies.texts[-1]


async def test_an_empty_answer_offers_to_keep_watching() -> None:
    """Искать больше негде, а новое появится — честный повод предложить слежение."""
    replies = Replies()
    await talk(MemoryStore(), filled(), items=[]).on_text(CLIENT, "ищу вертолёт", replies)

    assert replies.sent[-1].offer_subscription is True
    assert "звезда в месяц" in replies.texts[-1], "цену называем до нажатия, а не после"


async def test_results_the_client_may_not_like_offer_to_keep_watching() -> None:
    """Предложение появляется там же, где обратная связь «дорого» и «не то»."""
    replies = Replies()
    await talk(MemoryStore(), filled()).on_text(CLIENT, "ищу скутер", replies)

    cards = replies.sent[-1]
    assert "открыть оригинал" in cards.text
    assert cards.offer_subscription is True and cards.feedback


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


async def test_a_known_category_searches_without_asking_budget() -> None:
    """Перевёрнутый инвариант владельца: категория известна — ищем сразу.

    Раньше бот держал выдачу, пока не спросит бюджет; passport.md требует
    обратного — показать что есть рано и уточнять обратной связью. Бюджет до
    первой выдачи не звучит вовсе, карточки приходят сразу.
    """
    searched: list[Passport] = []

    async def find(passport: Passport) -> Found:
        searched.append(passport)
        return Found(items=[found("1")])

    replies = Replies()
    await talk(MemoryStore(), bike(), finder=find).on_text(CLIENT, "ищу скутер", replies)

    assert [reply.question for reply in replies.sent] == [None, None], "ни одного вопроса до выдачи"
    assert "Ищу" in replies.texts[0]
    assert "открыть оригинал" in replies.texts[1]
    assert searched, "поиск стартовал сразу, а не после вопроса про бюджет"


async def test_every_question_offers_a_way_out() -> None:
    """У любого вопроса есть «не важно» — теперь это вопрос категории.

    Единственный вопрос до выдачи — «что ищем?». У него, как и у прежнего вопроса
    про бюджет, последней кнопкой обязан стоять выход: клиента, которого
    допрашивают без права пропустить, теряют.
    """
    replies = Replies()
    await talk(MemoryStore(), vague()).on_text(CLIENT, "honda до 300", replies)

    question = replies.sent[0].question
    assert question is not None
    assert question.field == "category"
    assert [option.value for option in question.buttons][-1] == SKIP


async def test_at_most_one_question_before_the_search() -> None:
    """search-first: до выдачи звучит максимум ОДИН вопрос — категория.

    Раньше форма задавала до трёх вопросов подряд (бюджет, коробка, состояние),
    прежде чем помочь. Теперь блокирующее поле одно: без категории плана не
    собрать, а всё прочее уточняется уже на карточках. Ответил категорию — сразу
    выдача, второго вопроса до неё нет.
    """
    store = MemoryStore()
    talker = talk(store, vague())

    replies = Replies()
    await talker.on_text(CLIENT, "honda до 300", replies)
    assert replies.sent[0].question is not None
    assert replies.sent[0].question.field == "category"

    answered = Replies()
    await talker.on_answer(CLIENT, "cat", "motorbike", answered)
    assert [reply.question for reply in answered.sent] == [None, None], "второго вопроса нет"
    assert "Ищу" in answered.texts[0]

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.passport.category is Category.MOTORBIKE


async def test_skip_button_sends_us_searching() -> None:
    """«Не важно» на вопросе категории — разрешение искать по тому, что есть.

    Пропуск паспорт не меняет, значит и версии не создаёт. А раз категория —
    единственный блокирующий вопрос, за пропуском идёт уже выдача, а не новый
    вопрос: имя теста теперь буквально.
    """
    store = MemoryStore()
    talker = talk(store, vague())
    await talker.on_text(CLIENT, "honda до 300", Replies())

    replies = Replies()
    await talker.on_answer(CLIENT, "cat", SKIP, replies)

    assert "Ищу" in replies.texts[0], "после пропуска категории ищем по тому, что есть"
    assert [reply.question for reply in replies.sent] == [None, None], "нового вопроса нет"

    versions = [row.version for row in store.rows]
    assert versions == [1], "пропуск ничего не меняет — значит, и версии не создаёт"


async def test_words_instead_of_a_button_are_understood() -> None:
    """Ответ словами вместо кнопки понимается так же, как нажатие.

    Вопрос про бюджет теперь звучит из обратной связи («дорого», когда суммы ещё
    нет), но разбирается ответ тем же путём: «до 400 долларов» так же становится
    400 USD, как если бы нажали кнопку.
    """
    store = MemoryStore()
    talker = talk(store, bike())
    await talker.on_text(CLIENT, "ищу скутер", Replies())
    await talker.on_feedback(CLIENT, Feedback.PRICEY, Replies())

    replies = Replies()
    await talker.on_text(CLIENT, "до 400 долларов", replies)

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.passport.budget.max == 400
    assert current.passport.passport.budget.currency is Currency.USD
    assert [reply.question for reply in replies.sent] == [None, None], "после ответа — выдача"
    assert "Ищу" in replies.texts[0]


async def test_skip_in_words_works_like_the_button() -> None:
    """«да не важно» словами делает ровно то же, что кнопка «не важно».

    Вопрос теперь про категорию, но разбор пропуска тот же: слово-пропуск не
    меняет паспорт (версии нет) и, раз это единственный блокирующий вопрос,
    отправляет искать по тому, что уже известно.
    """
    store = MemoryStore()
    talker = talk(store, vague())
    await talker.on_text(CLIENT, "honda до 300", Replies())

    replies = Replies()
    await talker.on_text(CLIENT, "да не важно", replies)

    state = (await store.load(CLIENT)).state
    assert state.asked == ("category",), "категорию спросили и пропустили"
    assert [row.version for row in store.rows] == [1], "пропуск версии не создаёт"
    assert [reply.question for reply in replies.sent] == [None, None], "следом выдача, а не вопрос"
    assert "Ищу" in replies.texts[0]


async def test_text_that_is_not_an_answer_starts_a_new_request() -> None:
    """На вопрос «что ищем?» клиент отвечает новым запросом — это запрос, а не категория.

    Заодно проверяется, что счётчик вопросов у новой цепочки свой: прежний
    вопрос про категорию уже задан, и продолжись счёт — бот молча ушёл бы искать
    вместо того, чтобы спросить категорию новой просьбы.
    """
    store = MemoryStore()
    talker = talk(store, vague())
    await talker.on_text(CLIENT, "honda до 300", Replies())

    replies = Replies()
    await talker.on_text(CLIENT, "honda до 500", replies)

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.version == 1, "новая формулировка — новая цепочка, а не версия"
    assert current.passport.passport.raw_query == "honda до 500"
    assert current.state.asked == ("category",), "счётчик вопросов начался заново"
    assert replies.sent[0].question is not None
    assert replies.sent[0].question.field == "category"


async def test_repeating_the_same_words_keeps_what_was_collected() -> None:
    """Повтор той же формулировки — не новый запрос.

    Собранное обратной связью (бюджет и атрибуты) и счётчик вопросов повтор
    фразы обнулять не вправе: начни здесь новая цепочка — лимит уточнений
    обходился бы копипастом собственного сообщения.
    """
    store = MemoryStore()
    talker = talk(store, bike())
    await talker.on_text(CLIENT, "ищу скутер в нячанге", Replies())
    await talker.on_feedback(CLIENT, Feedback.WRONG, Replies())
    await talker.on_answer(CLIENT, "budget", "500", Replies())
    await talker.on_feedback(CLIENT, Feedback.WRONG, Replies())
    await talker.on_answer(CLIENT, "trans", "automatic", Replies())
    await talker.on_feedback(CLIENT, Feedback.WRONG, Replies())
    await talker.on_answer(CLIENT, "cond", "good", Replies())

    replies = Replies()
    await talker.on_text(CLIENT, "ищу скутер в нячанге", replies)

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.passport.budget.max == 500, "бюджет не потерян"
    assert current.passport.passport.attributes == {
        "transmission": "automatic",
        "condition": "good",
    }, "ответы на кнопки не потеряны"
    assert current.state.asked == (
        "budget.max",
        "attributes.transmission",
        "attributes.condition",
    ), "счётчик вопросов не начался заново"
    assert [reply.question for reply in replies.sent] == [None, None], "повтором лимит не обойти"
    assert "Ищу" in replies.texts[0]


async def test_a_shorter_wording_of_the_same_request_is_not_a_new_one() -> None:
    """Клиент повторяет короче — это та же просьба, а не смена темы.

    Собранный обратной связью бюджет сохраняется, а висящий вопрос уточнения
    (коробка) просто переспрашивается: короткая формулировка — не новый запрос.
    """
    store = MemoryStore()
    talker = talk(store, bike())
    await talker.on_text(CLIENT, "ищу скутер в нячанге", Replies())
    await talker.on_feedback(CLIENT, Feedback.WRONG, Replies())
    await talker.on_answer(CLIENT, "budget", "500", Replies())
    await talker.on_feedback(CLIENT, Feedback.WRONG, Replies())

    replies = Replies()
    await talker.on_text(CLIENT, "скутер в нячанге", replies)

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.passport.budget.max == 500
    assert current.state.asked[0] == "budget.max"
    assert replies.sent[0].question is not None
    assert replies.sent[0].question.field == "attributes.transmission", "висящий вопрос переспрошен"


@pytest.mark.parametrize(
    ("text", "slug", "shown"),
    [
        ("ищу скутер в Хойане", "hoi_an", "Хойан"),
        ("ищу скутер в Вунгтау", "vung_tau", "Вунгтау"),
        ("ищу скутер в Далате", "da_lat", "Далат"),
        ("ищу скутер в Ханое", "ha_noi", "Ханой"),
        ("ищу скутер в Сайгоне", "ho_chi_minh", "Хошимин"),
    ],
    ids=["hoi_an", "vung_tau", "da_lat", "ha_noi", "saigon"],
)
async def test_another_city_gets_an_answer_and_not_the_old_question(
    text: str, slug: str, shown: str
) -> None:
    """Смена города — ответ клиенту, а не переспрос висящего вопроса.

    Падало до правки: город вне справочника подставлялся городом по умолчанию,
    намерение, категория и город совпадали с прежним запросом, а общих слов
    выходило ровно 0.6 — то есть точно порог. `restates` отвечал `True`, и бот
    молча переспрашивал бюджет, будто клиент повторился.
    """
    store = MemoryStore()
    talker = Conversation(store, intake=RulesIntake, finder=nothing, recorder=FakeJournal())
    await talker.on_text(CLIENT, "ищу скутер в нячанге", Replies())

    replies = Replies()
    await talker.on_text(CLIENT, text, replies)

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.passport.city == slug, "город клиента, а не подставленный"
    assert current.passport.version == 1, "другой город — другой запрос, значит новая цепочка"
    assert [reply.question for reply in replies.sent] == [None], "старый вопрос не переспрошен"
    assert shown in replies.texts[0], "клиент назван городом, о котором спросил"
    assert all(name in replies.texts[0] for name in served_cities("ru")), "сказано, где ищем"


async def test_a_city_outside_the_dictionary_is_not_a_repeat_either() -> None:
    """Справочник тут ни при чём: слово новое — значит просьба новая.

    «Куангнгай» не знает ни один наш словарь, город подставляется прежним, и
    решает только половина со словами. Раньше она отвечала «повтор» ровно на
    пороге; теперь порога нет — есть слово, которого в прежней фразе не было.
    """
    store = MemoryStore()
    talker = Conversation(store, intake=RulesIntake, finder=nothing, recorder=FakeJournal())
    await talker.on_text(CLIENT, "ищу скутер в нячанге", Replies())

    replies = Replies()
    await talker.on_text(CLIENT, "ищу скутер в куангнгае", replies)

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.passport.raw_query == "ищу скутер в куангнгае", "новая цепочка"
    assert current.passport.version == 1, "новая просьба — новая цепочка версий, а не правка старой"
    assert "Ищу" in replies.texts[0], "новую просьбу ищем сразу, а не переспросом старого вопроса"


async def test_a_repeat_while_a_question_hangs_asks_it_again() -> None:
    """Клиент повторил запрос вместо ответа: вопрос остаётся тем же.

    Спросить следующий было бы обходом лимита через повтор, а промолчать —
    оставить клиента без вопроса, на который бот ждёт ответ. Висящий вопрос
    теперь — «что ищем?».
    """
    store = MemoryStore()
    talker = talk(store, vague())
    await talker.on_text(CLIENT, "honda до 300", Replies())

    replies = Replies()
    await talker.on_text(CLIENT, "honda до 300", replies)

    assert replies.sent[0].question is not None
    assert replies.sent[0].question.field == "category"
    assert (await store.load(CLIENT)).state.asked == ("category",), "вопрос всё тот же один"
    assert [row.version for row in store.rows] == [1], "повтор версий не плодит"


async def test_the_button_label_typed_by_hand_means_the_same_thing() -> None:
    """«любой, лишь бы ездил» — подпись кнопки `worn`, а не пропуск вопроса.

    Клиент читает кнопку и печатает её словами чаще, чем придумывает свои: раз
    кнопка ставит `worn`, то и её подпись обязана ставить `worn`. Вопрос о
    состоянии теперь приходит с обратной связью («не то»), разбор ответа тот же.
    """
    store = MemoryStore()
    talker = talk(
        store,
        bike(
            budget=Budget(max=400, currency=Currency.USD),
            attributes={"transmission": "automatic"},
        ),
    )
    await talker.on_text(CLIENT, "ищу скутер", Replies())

    asked = Replies()
    await talker.on_feedback(CLIENT, Feedback.WRONG, asked)
    assert asked.sent[0].question is not None
    assert asked.sent[0].question.field == "attributes.condition"

    await talker.on_text(CLIENT, "любой, лишь бы ездил", Replies())

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.passport.attributes["condition"] == "worn"
    assert current.passport.version == 2, "ответ словами — новая версия, а не пропуск"


async def test_a_bare_any_is_still_a_skip() -> None:
    """Разбор поля идёт первым, но «любой» сам по себе полю ничего не говорит.

    Пара к тесту выше: «любой, лишь бы ездил» — это `worn`, а голое «любой» —
    пропуск. Оба про вопрос состояния, который теперь приходит с обратной связью.
    """
    store = MemoryStore()
    talker = talk(
        store,
        bike(
            budget=Budget(max=400, currency=Currency.USD),
            attributes={"transmission": "automatic"},
        ),
    )
    await talker.on_text(CLIENT, "ищу скутер", Replies())
    await talker.on_feedback(CLIENT, Feedback.WRONG, Replies())

    await talker.on_text(CLIENT, "любой", Replies())

    assert [row.version for row in store.rows] == [1], "пропуск версии не создаёт"
    assert (await store.load(CLIENT)).state.asked == ("attributes.condition",)


async def test_stale_button_does_not_answer_twice() -> None:
    """Клавиатура остаётся в чате: второе нажатие не должно плодить версии.

    Вопрос про бюджет приходит с обратной связью; ответив на него, клиент видит
    уже выдачу, а прежняя клавиатура висит в чате. Повторное нажатие по ней — по
    вопросу, которого бот уже не ждёт, — не меняет ничего.
    """
    store = MemoryStore()
    talker = talk(store, bike())
    await talker.on_text(CLIENT, "ищу скутер", Replies())
    await talker.on_feedback(CLIENT, Feedback.WRONG, Replies())
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


async def test_second_wrong_in_a_row_has_nothing_left_to_ask() -> None:
    """Уточнений на обратной связи конечное число: второе «не то» подряд упирается в дно.

    «Не то» задаёт следующее незаполненное поле по информативности. Когда
    осталось одно (здесь — марка), первое нажатие спрашивает его, а второму
    подряд уточнять уже нечем: потолок обратной связи абсолютный, новым нажатием
    его не поднять.
    """
    store = MemoryStore()
    talker = talk(
        store,
        bike(
            budget=Budget(max=400, currency=Currency.USD),
            attributes={"transmission": "automatic", "condition": "good"},
        ),
    )
    await talker.on_text(CLIENT, "ищу скутер", Replies())

    first = Replies()
    await talker.on_feedback(CLIENT, Feedback.WRONG, first)
    assert first.sent[0].question is not None
    assert first.sent[0].question.field == "attributes.brand", "осталась одна незаполненная — марка"

    second = Replies()
    await talker.on_feedback(CLIENT, Feedback.WRONG, second)
    assert second.texts == [NOTHING_TO_REFINE], "второго уточнения нет — дно"


async def test_feedback_without_a_request_says_so() -> None:
    replies = Replies()
    await talk(MemoryStore(), filled()).on_feedback(CLIENT, Feedback.PRICEY, replies)

    assert replies.texts == [NO_REQUEST_YET]


# ── перезапуск процесса ─────────────────────────────────────────────────────


async def test_dialogue_survives_a_restart() -> None:
    """Бот перезапускается на каждом деплое — начатый разговор это переживает.

    Второй `Conversation` — это и есть перезапуск: у него нет ни памяти первого,
    ни его объектов. Всё, что осталось, — записи в хранилище. Висящий вопрос
    (теперь «что ищем?») обязан пережить перезапуск и принять ответ.
    """
    store = MemoryStore()
    before = talk(store, vague())
    await before.on_text(CLIENT, "honda до 300", Replies())

    after = talk(store, vague())
    replies = Replies()
    await after.on_text(CLIENT, "скутер", replies)

    current = await store.load(CLIENT)
    assert current.passport is not None
    assert current.passport.passport.category is Category.MOTORBIKE, "ответ лёг в начатый диалог"
    assert current.state.asked[0] == "category"
    assert [reply.question for reply in replies.sent] == [None, None], "после ответа — выдача"
    assert "Ищу" in replies.texts[0]


# ── проводка бота ───────────────────────────────────────────────────────────


async def test_start_explains_what_the_bot_does() -> None:
    hello = FakeMessage("/start")

    await handler.start(cast(Message, hello))

    assert len(hello.answers) == 1
    assert "объявлени" in hello.answers[0][0]


async def test_handler_draws_the_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Хендлер тонкий: берёт ответ разговора и рисует его кнопками.

    Единственный вопрос до выдачи — «что ищем?»; на нём и проверяется, что
    хендлер дорисовывает клавиатуре кнопку выхода «не важно».
    """
    talker = talk(MemoryStore(), vague())
    monkeypatch.setattr(handler, "conversation", lambda: talker)
    anonymous = FakeMessage("honda до 300")

    await handler.search(cast(Message, anonymous))
    assert anonymous.answers == [], "пост от имени канала паспорту не принадлежит"

    request = FakeMessage("honda до 300", from_user=FakeUser())
    await handler.search(cast(Message, request))

    text, keyboard = request.answers[0]
    assert "что ищем" in text.lower()
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "не важно, показать что есть" in labels


def test_answer_and_feedback_fit_the_callback_limit() -> None:
    """В callback_data влезает 64 байта — потому по проводу едут короткие ключи."""
    packed = AnswerCallback(code="trans", value="automatic").pack()
    feedback = FeedbackCallback(kind=Feedback.PRICEY.value).pack()

    assert len(packed.encode()) <= 64
    assert len(feedback.encode()) <= 64
    assert AnswerCallback.unpack(packed).value == "automatic"


def test_cyrillic_value_is_measured_in_bytes_not_characters() -> None:
    """Лимит именно байтовый, и проверять его надо тем, где байт больше символа.

    На латинице длина в символах и в байтах совпадает, поэтому кириллица —
    единственный вход, который отличит верный подсчёт от посимвольного: 27 букв
    ещё влезают (63 байта при 36 символах), 28-я уже нет (65 байт при 37).
    """
    fits = AnswerCallback(code="cond", value="я" * 27).pack()

    assert len(fits) < len(fits.encode()), "символов меньше, чем байтов"
    assert len(fits.encode()) == 63

    with pytest.raises(ValueError, match="too long"):
        AnswerCallback(code="cond", value="я" * 28).pack()


def test_reply_without_buttons_has_no_keyboard() -> None:
    assert markup(Reply("просто текст")) is None


def test_dispatcher_knows_the_dialog() -> None:
    dispatcher = bot_app.build_dispatcher()

    assert [router.name for router in dispatcher.sub_routers] == ["search"]


async def test_appending_a_word_does_not_buy_three_more_questions() -> None:
    """Лимит трёх вопросов нельзя обойти дописыванием служебного слова.

    Замер на живой базе: «ищу скутер в нячанге», потом «…а», потом «…а б», между
    ними по три «не важно» — девять вопросов вместо трёх и три цепочки версий
    вместо одной. Каждая новая цепочка обнуляла и собранные ответы, и счётчик.
    """
    store = MemoryStore()
    talker = talk(store, bike())
    asked = 0

    for text in ("ищу скутер в нячанге", "ищу скутер в нячанге а", "ищу скутер в нячанге а б"):
        replies = Replies()
        await talker.on_text(CLIENT, text, replies)
        asked += sum(1 for reply in replies.sent if reply.question is not None)
        for code in ("budget", "trans", "cond"):
            skipped = Replies()
            await talker.on_answer(CLIENT, code, SKIP, skipped)
            asked += sum(1 for reply in skipped.sent if reply.question is not None)

    assert asked <= 3, f"задано {asked} вопросов — лимит обойден дописыванием слова"


# ── журнал и учёт расходов: ход целиком ─────────────────────────────────────
#
# Раньше эти тесты шли через хендлер: журнал жил там. Ход теперь принадлежит
# разговору (хендлер не знает ни разбора, ни вопросов, ни поиска), поэтому и
# проверяются они здесь — на том же уровне, где журнал открывается и закрывается.


class FakeJournal:
    """Журнал без базы: помнит, что бот записал бы о диалоге.

    Подменяется целиком, потому что настоящий пошёл бы в Postgres. Проверять
    здесь надо диалог, а запись в базу проверяют тесты репозиториев.
    """

    def __init__(self) -> None:
        self.opened: list[tuple[int, str]] = []
        self.answers: list[str] = []
        self.closed: list[dict[str, Any]] = []

    async def open_request(
        self, tg_user_id: int, text: str, *, username: str | None = None
    ) -> journal.OpenRequest:
        self.opened.append((tg_user_id, text))
        return journal.OpenRequest(user_id=1, request_id=len(self.opened))

    async def log_answer(self, opened: journal.OpenRequest | None, text: str) -> None:
        self.answers.append(text)

    async def close_request(self, opened: journal.OpenRequest | None, **kwargs: Any) -> None:
        self.closed.append({"request_id": None if opened is None else opened.request_id, **kwargs})


@pytest.fixture
def recorded() -> FakeJournal:
    """Журнал без базы — его же подставляем разговору явно."""
    return FakeJournal()


async def test_journal_records_the_whole_turn(recorded: FakeJournal) -> None:
    """Дашборд показывает и вопрос, и ответы, и время по этапам."""
    replies = Replies()

    await talk(MemoryStore(), filled(), recorder=recorded).on_text(CLIENT, "ищу скутер", replies)

    assert recorded.opened == [(CLIENT.tg_user_id, "ищу скутер")]
    assert recorded.answers == [reply.text for reply in replies.sent]
    closed = recorded.closed[0]
    assert closed["result_count"] == 1
    assert closed.get("error") is None
    assert "intake_ms" in closed["stages"]


async def test_a_failed_search_is_closed_with_the_reason(recorded: FakeJournal) -> None:
    """Упавший запрос обязан остаться в журнале — иначе видно только удачные."""

    async def boom(_passport: Passport) -> Found:
        raise RuntimeError("источник отдал не то")

    await talk(MemoryStore(), filled(), finder=boom, recorder=recorded).on_text(
        CLIENT, "ищу скутер", Replies()
    )

    assert "RuntimeError" in recorded.closed[0]["error"]
    assert recorded.closed[0]["result_count"] == 0


async def test_a_broken_turn_is_still_closed(recorded: FakeJournal) -> None:
    """Ошибка ВНЕ поиска тоже закрывает запись, и причина в ней названа.

    Иначе в дашборде остаётся вечно открытый запрос, и статистика показывает
    ровно обратную картину: видны только те ходы, которые дошли до конца.
    """

    class BrokenStore(MemoryStore):
        async def start(self, dialogue: Any, passport: Passport) -> Any:
            raise RuntimeError("база отвалилась на записи паспорта")

    talker = talk(BrokenStore(), filled(), recorder=recorded)

    with pytest.raises(RuntimeError):
        await talker.on_text(CLIENT, "ищу скутер", Replies())

    assert "RuntimeError" in recorded.closed[0]["error"]


async def test_the_plan_reaches_the_journal(recorded: FakeJournal) -> None:
    """Доля фолбэков считается по этому полю — потерять его нельзя.

    Искатель отчитывается о плане именно поэтому: разговор о плане не знает, а
    дашборд по нему видит, что брокер лежит.
    """

    async def fallback_found(_passport: Passport) -> Found:
        return Found(items=[found("1")], fallback=True, sources=("chotot",), stages={"plan_ms": 1})

    await talk(MemoryStore(), filled(), finder=fallback_found, recorder=recorded).on_text(
        CLIENT, "ищу скутер", Replies()
    )

    closed = recorded.closed[0]
    assert closed["plan_fallback"] is True
    assert closed["sources"] == ["chotot"]
    assert closed["stages"]["plan_ms"] == 1


async def test_broker_calls_are_scoped_to_the_request(recorded: FakeJournal) -> None:
    """Расход, записанный во время поиска, принадлежит этому запросу."""
    seen: list[int | None] = []

    async def watching(_passport: Passport) -> Found:
        seen.append(usage.current_request_id())
        return Found(items=[])

    await talk(MemoryStore(), filled(), finder=watching, recorder=recorded).on_text(
        CLIENT, "ищу скутер", Replies()
    )

    assert seen == [1]
    # За пределами хода область снимается: следующий вызов брокера не должен
    # приписаться прошлому клиенту.
    assert usage.current_request_id() is None


def test_the_real_journal_fits_the_recorder_protocol() -> None:
    """Настоящий журнал подходит разговору по всем трём методам.

    Разговор берёт модуль `journal` через `cast`: mypy видит `Module`, а не
    класс, и соответствие протоколу на боевом пути не проверяет никто. Разойдись
    подпись — сломался бы прод, где подставлен именно модуль, а не заглушка.
    """
    for name in ("open_request", "log_answer", "close_request"):
        assert callable(getattr(journal, name)), f"журнал не умеет {name}"

    assert set(inspect.signature(journal.open_request).parameters) == {
        "tg_user_id",
        "text",
        "username",
    }
    assert set(inspect.signature(journal.log_answer).parameters) == {"opened", "text"}
    # Поля закрытия сверяются поимённо: разговор передаёт их именованно, и
    # переименованное поле сломало бы прод молча — заглушка-то принимает всё.
    assert set(inspect.signature(journal.close_request).parameters) == {
        "opened",
        "stages",
        "result_count",
        "plan_fallback",
        "sources",
        "error",
    }


# ── журнал: ход считается ходом, чем бы его ни начали ───────────────────────
#
# Живой след 01.09.2026: запрос №2 отдал клиенту пять карточек Chotot, а в
# `client_requests` у него `result_count = 0` и пустой `sources`; расход модели
# на кнопочном поиске приехал с `request_id = NULL`. Причина была структурная:
# `_Turn` ехал аргументом, и четыре места из шести его теряли.


async def test_a_restated_query_still_reports_what_it_found(recorded: FakeJournal) -> None:
    """Повтор той же просьбы ищет по-настоящему — значит и в журнале не ноль.

    Именно этот путь (`_restated`) и врал в проде: карточки уходили, журнал
    писал ноль. Тест повторяет его дословно — два одинаковых сообщения подряд.
    """
    store = MemoryStore()
    talker = talk(store, filled(), recorder=recorded)

    await talker.on_text(CLIENT, "ищу скутер", Replies())
    second = Replies()
    await talker.on_text(CLIENT, "ищу скутер", second)

    assert any("открыть оригинал" in text for text in second.texts), "карточки клиенту ушли"
    assert recorded.closed[1]["result_count"] == 1, "а журнал обязан это увидеть"


async def test_a_button_press_opens_its_own_journal_entry(recorded: FakeJournal) -> None:
    """Нажатие кнопки — полноценный поиск: те же деньги, та же запись."""
    store = MemoryStore()
    talker = talk(store, bike(), recorder=recorded)
    await talker.on_text(CLIENT, "ищу скутер", Replies())
    opened_before = len(recorded.opened)

    replies = Replies()
    await talker.on_answer(CLIENT, "budget", "500", replies)

    assert len(recorded.opened) == opened_before + 1, "у кнопки обязан быть свой запрос"
    assert recorded.opened[-1][1].startswith("кнопка:"), recorded.opened[-1]


async def test_a_feedback_press_reports_its_results(recorded: FakeJournal) -> None:
    """«Дорого» перезапускает подбор — и он тоже обязан попасть в журнал."""
    store = MemoryStore()
    talker = talk(store, filled(), recorder=recorded)
    await talker.on_text(CLIENT, "ищу скутер", Replies())

    await talker.on_feedback(CLIENT, Feedback.PRICEY, Replies())

    assert recorded.closed[-1]["result_count"] == 1
    assert recorded.opened[-1][1] == f"кнопка: {Feedback.PRICEY.value}"


def test_the_search_cannot_be_called_without_its_turn() -> None:
    """Структурная защита: у `_search` нет параметра, который можно забыть.

    Поведенческие тесты выше проверяют шесть известных путей. Седьмой напишут
    завтра и снова забудут передать ход — если передавать будет что. Поэтому
    ход берётся из contextvar, а этот тест сторожит саму возможность: появился
    параметр `turn` — значит вернулась и возможность его потерять.
    """
    for name in ("_search", "_ask_or_search"):
        method = getattr(Conversation, name)
        assert "turn" not in inspect.signature(method).parameters, (
            f"{name} снова принимает ход аргументом — его снова забудут передать"
        )
