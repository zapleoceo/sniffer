"""Что бот говорит про вытесненный из списка поиск: мониторинг, экранирование, названия.

Текст строится по настоящему состоянию подписки вытесненного поиска (подделка хранилища
принимает его в `MemoryStore.monitoring`), а название идёт в сообщение с HTML и обязано
быть экранировано.
"""

from __future__ import annotations

import pytest

from sniffer.bot.conversation import Conversation
from sniffer.domain.threads import MAX_LIVE_THREADS
from sniffer.simulation.stubs import MemoryStore
from tests.thread_support import CLIENT, Replies, talk


async def _fill_five(store: MemoryStore, talker: Conversation, *, first: str) -> None:
    """Пять поисков: первый — как сказано, остальные — скутеры в других городах."""
    await talker.start_new(CLIENT)
    await talker.on_text(CLIENT, first, Replies())
    for city in ("хойане", "вунгтау", "далате", "ханое"):
        await talker.start_new(CLIENT)
        await talker.on_text(CLIENT, f"ищу скутер в {city}", Replies())
    assert len(await store.live_threads(await store.load(CLIENT))) == MAX_LIVE_THREADS


async def _sixth(talker: Conversation) -> Replies:
    replies = Replies()
    await talker.start_new(CLIENT)
    await talker.on_text(CLIENT, "сниму квартиру в нячанге", replies)
    return replies


def _notice(replies: Replies) -> str:
    said = [text for text in replies.texts if "из него убран" in text]
    assert said, f"про вытеснение не сказано: {replies.texts}"
    return said[0]


@pytest.mark.parametrize(
    ("state", "phrase"),
    [
        ("active", "мониторинг продолжает работать"),
        ("paused", "мониторинг на паузе"),
    ],
)
async def test_the_notice_names_the_real_monitoring_of_the_pushed_out_search(
    state: str, phrase: str
) -> None:
    """Про мониторинг говорится то, что есть: подписка идёт — «продолжает», на паузе — «на паузе».

    Прежний текст всегда утверждал «мониторинг работает», хотя подписок у
    вытесненных поисков на проде не было вовсе: человек ждал уведомлений, которых нет.
    """
    store = MemoryStore()
    talker = talk(store)
    await _fill_five(store, talker, first="ищу скутер в нячанге")
    store.monitoring[store.rows[0].root] = state

    assert phrase in _notice(await _sixth(talker))


@pytest.mark.parametrize("state", [None, "expired"], ids=["no_subscription", "expired"])
async def test_the_notice_is_silent_about_monitoring_that_is_not_there(state: str | None) -> None:
    """Подписки нет или она истекла — слова про мониторинг лгали бы."""
    store = MemoryStore()
    talker = talk(store)
    await _fill_five(store, talker, first="ищу скутер в нячанге")
    if state is not None:
        store.monitoring[store.rows[0].root] = state

    notice = _notice(await _sixth(talker))

    assert "мониторинг" not in notice
    assert notice.endswith("Сам поиск сохранён.")


async def test_the_notice_survives_a_title_with_markup_characters() -> None:
    """Название без предмета берётся из слов клиента, и «<» в нём не должен ронять ответ.

    Bot API в режиме HTML отвергает сообщение с сырым «<» целиком, и человек не
    получает ничего — ни уведомления, ни запущенного поиска (отказ вылетал бы из
    `_open` уже ПОСЛЕ создания ветки).
    """
    store = MemoryStore()
    talker = talk(store)
    await _fill_five(store, talker, first="что-нибудь <300$ & <b>")

    notice = _notice(await _sixth(talker))

    assert "&lt;300$ &amp; &lt;b&gt;" in notice
    assert "<300" not in notice
    assert "<b>" not in notice


async def test_the_notice_tells_two_twin_titles_apart_by_budget() -> None:
    """Два поиска «Мотобайк, Нячанг» в списке различимы — и вытесненный назван так же.

    Иначе уведомление называло бы строку, которая в списке осталась у соседа.
    """
    store = MemoryStore()
    talker = talk(store)
    await talker.start_new(CLIENT)
    await talker.on_text(CLIENT, "ищу скутер в нячанге до 300 долларов", Replies())
    await talker.start_new(CLIENT)
    await talker.on_text(CLIENT, "ищу скутер в нячанге до 1000 долларов", Replies())
    for city in ("хойане", "вунгтау", "далате"):
        await talker.start_new(CLIENT)
        await talker.on_text(CLIENT, f"ищу скутер в {city}", Replies())

    assert "до 300 USD" in _notice(await _sixth(talker))
