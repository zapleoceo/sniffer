"""Ветки запросов: переключение, лимит и чужой контекст, в который нельзя уехать.

Хранилище — `simulation.stubs.MemoryStore`: цепочки версий оно считает
по-настоящему, и именно поэтому тест «уточнение ушло в правильную ветку»
проверяет бота, а не подделку. Третьей копии словарного хранилища здесь нет
намеренно (см. докстринг `stubs`).
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from aiogram.types import Message

from sniffer.bot import query_menu, threads
from sniffer.bot.conversation import Conversation, Found, Reply
from sniffer.bot.handlers import search as handler
from sniffer.bot.store import Client
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.domain.records import QueryOverview
from sniffer.domain.threads import MAX_LIVE_THREADS, pushed_out
from sniffer.search.intake_rules import parse_query
from sniffer.simulation.stubs import MemoryStore, SilentJournal

CLIENT = Client(tg_user_id=42, username="dima")


class Replies:
    def __init__(self) -> None:
        self.sent: list[Reply] = []

    async def __call__(self, reply: Reply) -> None:
        self.sent.append(reply)

    @property
    def texts(self) -> list[str]:
        return [reply.text for reply in self.sent]


async def nothing(_passport: Passport) -> Found:
    return Found(items=[])


def talk(store: MemoryStore) -> Conversation:
    """Разговор на правилах разбора: категорию и город берём из слов клиента.

    Заранее заданный паспорт здесь не годится принципиально — он подменил бы
    ровно те поля, по которым ветки и различаются.
    """
    return Conversation(store, intake=lambda: _Rules(), finder=nothing, recorder=SilentJournal())


class _Rules:
    async def parse(self, text: str) -> Passport:
        return parse_query(text)


def bike(**overrides: object) -> Passport:
    fields: dict[str, object] = {
        "intent": Intent.BUY,
        "category": Category.MOTORBIKE,
        "city": "nha_trang",
        "budget": Budget(max=400, currency=Currency.USD),
        "raw_query": "ищу скутер в Нячанге",
    }
    fields.update(overrides)
    return Passport(**fields)  # type: ignore[arg-type]


# ── переключение между ветками ──────────────────────────────────────────────


async def test_new_starts_its_own_thread_and_leaves_the_old_one_alone() -> None:
    """`/new` — отдельная ветка, а прежняя остаётся со всем, что в ней собрано."""
    store = MemoryStore()
    talker = talk(store)
    await talker.on_text(CLIENT, "ищу скутер в нячанге до 400 долларов", Replies())
    scooter = (await store.load(CLIENT)).passport
    assert scooter is not None

    await talker.start_new(CLIENT)
    await talker.on_text(CLIENT, "сниму квартиру в нячанге", Replies())

    flat = (await store.load(CLIENT)).passport
    assert flat is not None
    assert flat.root != scooter.root, "новая ветка, а не версия прежней"
    assert flat.version == 1
    assert flat.passport.category is Category.APARTMENT
    after = await store.load(CLIENT)
    live = {item.root for item in await store.live_threads(after)}
    assert live == {scooter.root, flat.root}, "прежняя ветка не удалена"
    assert after.passport is not None
    assert after.passport.root == flat.root, "активна та, которую только что открыли"


async def test_switching_back_restores_the_other_thread_untouched() -> None:
    """Выбор ветки возвращает её паспорт целиком, а не последний по времени."""
    store = MemoryStore()
    talker = talk(store)
    await talker.on_text(CLIENT, "ищу скутер в нячанге до 400 долларов", Replies())
    scooter = (await store.load(CLIENT)).passport
    assert scooter is not None
    await talker.start_new(CLIENT)
    await talker.on_text(CLIENT, "сниму квартиру в нячанге", Replies())

    back = await store.select(await store.load(CLIENT), scooter.root)

    assert back.passport is not None
    assert back.passport.root == scooter.root
    assert back.passport.passport.category is Category.MOTORBIKE
    assert back.passport.passport.budget.max == 400, "бюджет прежней ветки на месте"


async def test_a_refinement_lands_in_the_active_thread_and_not_the_neighbour() -> None:
    """Главная жалоба владельца: уточнение бюджета уезжало в чужой паспорт.

    Две ветки, активна вторая — значит «до 500» уточняет ВТОРУЮ. Проверяется не
    только то, куда бюджет попал, но и то, что в соседней ветке он не изменился:
    «попал куда надо» и «не попал куда не надо» — два разных утверждения, и
    второе как раз и ломалось.
    """
    store = MemoryStore()
    talker = talk(store)
    await talker.on_text(CLIENT, "ищу скутер в нячанге до 400 долларов", Replies())
    scooter = (await store.load(CLIENT)).passport
    assert scooter is not None
    await talker.start_new(CLIENT)
    await talker.on_text(CLIENT, "сниму квартиру в нячанге за 10 млн донгов", Replies())
    flat = (await store.load(CLIENT)).passport
    assert flat is not None

    await talker.on_text(CLIENT, "до 500", Replies())

    versions = {row.root: row for row in store.rows if row.is_current}
    assert versions[flat.root].passport.budget.max == 500, "уточнение ушло в активную ветку"
    assert versions[flat.root].passport.category is Category.APARTMENT
    assert versions[scooter.root].passport.budget.max == 400, "соседняя ветка не тронута"
    assert versions[scooter.root].version == 1, "и новой версии у неё не появилось"


async def test_two_parallel_searches_keep_their_own_question_counters() -> None:
    """Счётчик вопросов у ветки свой: он собирается из событий её цепочки.

    Иначе соседний поиск доедал бы лимит уточнений — человек платит вопросами за
    один запрос, а тратились бы они на два.
    """
    store = MemoryStore()
    talker = talk(store)
    # «что-нибудь» категорией не читается — значит, бот спросит «что ищем?» и
    # потратит на это вопрос ИМЕННО этой ветки.
    await talker.on_text(CLIENT, "ищу что-нибудь в нячанге", Replies())
    vague = (await store.load(CLIENT)).passport
    assert vague is not None
    assert (await store.load(CLIENT)).state.asked == ("category",)

    await talker.start_new(CLIENT)
    await talker.on_text(CLIENT, "ищу скутер в нячанге", Replies())

    fresh = await store.load(CLIENT)
    assert fresh.state.asked == (), "у новой ветки счётчик свой и пустой"
    back = await store.select(fresh, vague.root)
    assert back.state.asked == ("category",), "у прежней ветки свой вопрос не потерян"


async def test_new_overrules_a_word_for_word_repeat() -> None:
    """Дословный повтор после `/new` — новая ветка, а не «он повторился».

    Это и есть «`restates` работает ВНУТРИ ветки»: эвристика решает, что делать с
    сообщением в активной ветке, и не вправе отменить явное решение человека
    начать новую. Без флага `restates` отвечал бы здесь «повтор» — слова те же до
    буквы — и второй ветки не появилось бы вовсе.
    """
    store = MemoryStore()
    talker = talk(store)
    await talker.on_text(CLIENT, "ищу скутер в нячанге", Replies())

    await talker.start_new(CLIENT)
    await talker.on_text(CLIENT, "ищу скутер в нячанге", Replies())

    roots = {row.root for row in store.rows}
    assert len(roots) == 2, "те же слова, но ветка новая"
    assert [row.version for row in store.rows] == [1, 1], "ни одна ветка не получила версию"


async def test_an_armed_new_search_is_spent_once() -> None:
    """Флаг `/new` тратится первым же сообщением, а не остаётся взведённым.

    Иначе каждое следующее уточнение открывало бы ветку, и диалог распадался бы
    на цепочку одноразовых запросов — ровно то, от чего ветки и спасают.
    """
    store = MemoryStore()
    talker = talk(store)
    await talker.start_new(CLIENT)
    await talker.on_text(CLIENT, "ищу скутер в нячанге", Replies())
    assert (await store.load(CLIENT)).starting_new is False

    await talker.on_text(CLIENT, "до 500", Replies())

    assert {row.root for row in store.rows} == {1}, "второе сообщение уточнило ветку"
    assert [row.version for row in store.rows] == [1, 2]


async def test_selecting_a_thread_disarms_a_pending_new_search() -> None:
    """Человек сказал `/new`, а потом выбрал ветку кнопкой — ветку он и выбрал."""
    store = MemoryStore()
    talker = talk(store)
    await talker.on_text(CLIENT, "ищу скутер в нячанге", Replies())
    chosen = (await store.load(CLIENT)).passport
    assert chosen is not None

    await talker.start_new(CLIENT)
    await store.select(await store.load(CLIENT), chosen.root)
    await talker.on_text(CLIENT, "до 500", Replies())

    assert {row.root for row in store.rows} == {chosen.root}, "новой ветки не появилось"
    assert store.rows[-1].passport.budget.max == 500


# ── лимит веток ─────────────────────────────────────────────────────────────


def test_the_limit_names_the_thread_that_leaves_the_list() -> None:
    """Правило «кто вытесняется» проверяется без базы: это чистое знание."""
    threads_in_work = [
        QueryOverview(root=root, passport=bike(raw_query=f"запрос {root}"))
        for root in range(1, MAX_LIVE_THREADS + 1)
    ]

    assert pushed_out(threads_in_work[:-1]) is None, "мест ещё хватает"
    leaving = pushed_out(threads_in_work)
    assert leaving is not None
    assert leaving.root == threads_in_work[-1].root, "уходит самая старая, то есть последняя"


async def test_a_sixth_search_pushes_the_oldest_out_of_the_list_without_losing_it() -> None:
    """Предел — на видимые ветки. Вытесненная не удаляется, и о ней говорят.

    Молчаливое вытеснение было бы той же бедой, что молчаливое угадывание ветки:
    человек не узнал бы, почему его поиск исчез из `/requests`.
    """
    store = MemoryStore()
    talker = talk(store)
    cities = ["нячанге", "хойане", "вунгтау", "далате", "ханое"]
    for city in cities:
        await talker.start_new(CLIENT)
        await talker.on_text(CLIENT, f"ищу скутер в {city}", Replies())
    oldest = store.rows[0]
    assert len(await store.live_threads(await store.load(CLIENT))) == MAX_LIVE_THREADS

    replies = Replies()
    await talker.start_new(CLIENT)
    await talker.on_text(CLIENT, "сниму квартиру в нячанге", replies)

    live = await store.live_threads(await store.load(CLIENT))
    assert len(live) == MAX_LIVE_THREADS, "в работе остаётся предел, а не шесть"
    assert oldest.root not in {item.root for item in live}, "вытеснена самая старая"
    said = [text for text in replies.texts if threads.title(oldest.passport) in text]
    assert said, "вытеснение названо человеку"
    assert str(MAX_LIVE_THREADS) in said[0], "предел в тексте подставлен, а не вписан словом"
    assert any(row.root == oldest.root for row in store.rows), "вытесненная ветка не удалена"
    back = await store.select(await store.load(CLIENT), oldest.root)
    assert back.passport is not None
    assert back.passport.root == oldest.root, "к вытесненной ветке можно вернуться"


async def test_a_sixth_search_within_the_limit_says_nothing_about_crowding() -> None:
    """Пока места хватает, про вытеснение молчим: лишняя строка читается как отказ."""
    store = MemoryStore()
    talker = talk(store)
    await talker.start_new(CLIENT)
    replies = Replies()
    await talker.on_text(CLIENT, "ищу скутер в нячанге", replies)

    assert all("ушёл из списка" not in text for text in replies.texts)


# ── заголовок ветки ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("passport", "expected"),
    [
        (bike(attributes={"body_type": "tay_ga"}), "Скутер, Нячанг"),
        (bike(), "Мотобайк, Нячанг"),
        (bike(category=Category.APARTMENT, intent=Intent.RENT), "Квартира, Нячанг"),
        (bike(attributes={"brand": "honda", "model": "lead"}), "Мотобайк Honda Lead, Нячанг"),
        (bike(city=None), "Мотобайк"),
    ],
    ids=["scooter", "motorbike", "apartment", "brand_and_model", "no_city"],
)
def test_the_thread_title_names_the_subject_and_the_city(passport: Passport, expected: str) -> None:
    assert threads.title(passport) == expected


def test_a_title_without_a_category_falls_back_to_the_words_said() -> None:
    """Категории нет — звать ветку нечем, кроме сказанного: пустая кнопка хуже."""
    assert threads.title(bike(category=None, raw_query="honda до 300")) == "Honda до 300"


def test_a_long_title_is_cut_to_fit_a_telegram_button() -> None:
    long = bike(attributes={"brand": "honda", "model": "super cub c125 final edition"})

    label = threads.title(long)

    assert len(label) <= threads.TITLE_LIMIT
    assert label.endswith("…")


def test_the_title_does_not_follow_the_last_wording() -> None:
    """Подпись кнопки не прыгает от правок: иначе ветку не найти глазами.

    Формулировка меняется на каждом уточнении («до 500», «не скутер, а
    мотоцикл»), а человек ищет в списке ту строку, которую запомнил.
    """
    first = bike(raw_query="ищу скутер в Нячанге до 400 долларов")
    edited = bike(
        raw_query="до 500\nПоследнее уточнение (заменяет прежние условия): до 500",
        budget=Budget(max=500, currency=Currency.USD),
    )

    assert threads.title(first) == threads.title(edited)


# ── команда и кнопка ────────────────────────────────────────────────────────


async def test_the_new_command_arms_a_thread_and_searches_when_given_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/new` без текста спрашивает, с текстом — ищет. Взводится оба раза.

    Взводится и с текстом тоже: иначе два входа в одну ветку расходились бы в
    поведении, и `/new скутер` после разговора про квартиру дал бы версию
    квартиры.
    """
    armed: list[int] = []
    searched: list[str] = []

    class Talker:
        async def start_new(self, client: Client) -> None:
            armed.append(client.tg_user_id)

        async def on_text(self, _client: Client, text: str, _send: Any) -> None:
            searched.append(text)

    monkeypatch.setattr(handler, "Message", _FakeMessage)
    monkeypatch.setattr(handler, "conversation", lambda: Talker())

    bare = _FakeMessage("/new")
    await handler.new_request(cast(Message, bare))
    with_words = _FakeMessage("/new ищу скутер в Нячанге")
    await handler.new_request(cast(Message, with_words))

    assert armed == [42, 42]
    assert searched == ["ищу скутер в Нячанге"], "текст команды ушёл в поиск без слова «/new»"
    assert bare.answers[0] == threads.ASK_WHAT
    assert with_words.answers == [], "со словами спрашивать нечего"


async def test_the_menu_lists_live_threads_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Меню спрашивает у базы ветки в работе, и предел стоит в запросе, а не здесь."""
    asked: list[int] = []

    class Repo:
        def __init__(self, _session: object) -> None:
            pass

        async def list_queries(self, user_id: int, *, limit: int = MAX_LIVE_THREADS) -> list[Any]:
            asked.append(limit)
            return []

    class Users:
        def __init__(self, _session: object) -> None:
            pass

        async def get_or_create(self, _tg_user_id: int, **_kwargs: object) -> Any:
            return type("U", (), {"id": 1})()

    monkeypatch.setattr(query_menu, "PassportRepository", Repo)
    monkeypatch.setattr(query_menu, "UserRepository", Users)
    monkeypatch.setattr(query_menu, "session_scope", _FakeSessions)

    assert await query_menu.list_for(CLIENT) == []
    assert asked == [MAX_LIVE_THREADS], "предел берётся из домена, а не из числа в меню"


class _FakeMessage:
    """Ровно то, что хендлер трогает у сообщения команды."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.from_user = type("U", (), {"id": 42, "username": "dima"})()
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs: object) -> None:
        self.answers.append(text)


class _FakeSessions:
    """`session_scope()` без Postgres: коммит есть, базы нет."""

    async def __aenter__(self) -> _FakeSessions:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def commit(self) -> None:
        return None
