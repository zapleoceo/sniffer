"""Поиски (ветки): переключение, лимит и вытеснение из списка.

Хранилище — `simulation.stubs.MemoryStore`: цепочки версий оно считает
по-настоящему, и именно поэтому тест «уточнение ушло в правильную ветку»
проверяет бота, а не подделку. Третьей копии словарного хранилища здесь нет
намеренно (см. докстринг `stubs`); что подделка делает так же, как база,
доказывают контрактные тесты (`test_store_contract.py`). Что сказано про вытесненный
поиск — `test_thread_notice.py`, гонка двух сообщений после `/new` — `test_thread_race.py`.
"""

from __future__ import annotations

from sniffer.bot import threads
from sniffer.domain.passport import Category
from sniffer.domain.records import QueryOverview
from sniffer.domain.threads import MAX_LIVE_THREADS, pushed_out
from sniffer.simulation.stubs import MemoryStore
from tests.thread_support import CLIENT, Replies, bike, talk

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

    assert all("из него убран" not in text for text in replies.texts)
