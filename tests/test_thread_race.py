"""Два сообщения подряд после `/new`: одно взведение — один поиск.

В бою разбор — вызов модели (p50 2.6 с, p90 12.5 с), и всё это время флаг в базе
взведён: второе сообщение успевает прочитать его до того, как первое открыло поиск.
Тесты идут на подделке хранилища с настоящим `asyncio.gather`; то же на настоящей базе
— `test_thread_repository.py`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sniffer.bot.conversation import Conversation
from sniffer.domain.passport import Category, Passport
from sniffer.search.intake_rules import parse_query
from sniffer.simulation.stubs import MemoryStore, SilentJournal
from tests.thread_support import CLIENT, Replies, Rules, bike, nothing, talk


class _SlowRules:
    """Разбор, занимающий время: пока он идёт, соседнее сообщение успевает прийти.

    В бою разбор — вызов модели (p50 2.6 с, p90 12.5 с), и это окно, в котором два
    быстрых сообщения читают взведённый флаг оба.
    """

    def __init__(self, delays: dict[str, float]) -> None:
        self._delays = delays

    async def parse(self, text: str) -> Passport:
        await asyncio.sleep(self._delays.get(text, 0.0))
        return parse_query(text)


def _talk_slowly(store: MemoryStore, delays: dict[str, float]) -> Conversation:
    return Conversation(
        store, intake=lambda: _SlowRules(delays), finder=nothing, recorder=SilentJournal()
    )


@pytest.mark.parametrize(
    "delays",
    [
        {"сниму квартиру в нячанге": 0.01, "до 10 млн донгов": 0.05},
        {"сниму квартиру в нячанге": 0.05, "до 10 млн донгов": 0.01},
    ],
    ids=["substance_first", "number_first"],
)
async def test_two_quick_messages_after_new_open_one_thread(delays: dict[str, float]) -> None:
    """Флаг тратится атомарно: на `/new` рождается ОДНА ветка, а не две и не три.

    До правки разбор шёл с задержкой, оба сообщения видели взведённый флаг и
    открывали по ветке: «сниму квартиру» и безымянная ветка с «до 10 млн донгов»,
    которая спрашивала «Что ищем?» и занимала одно из пяти мест. Порядок, в
    котором разбор кончил работу, от порядка прихода сообщений не зависит, поэтому
    проверяются оба: и когда первым открывает содержательное, и когда число.
    """
    store = MemoryStore()
    talker = _talk_slowly(store, delays)
    await talker.on_text(CLIENT, "ищу скутер в нячанге", Replies())
    await talker.start_new(CLIENT)

    await asyncio.gather(
        talker.on_text(CLIENT, "сниму квартиру в нячанге", Replies()),
        talker.on_text(CLIENT, "до 10 млн донгов", Replies()),
    )

    roots = {row.root for row in store.rows}
    assert len(roots) == 2, f"скутер и ОДНА новая ветка, а вышло {len(roots)}: {sorted(roots)}"
    current = {row.root: row for row in store.rows if row.is_current}
    new_root = max(roots)
    assert current[new_root].passport.category is Category.APARTMENT
    assert current[new_root].passport.budget.max == 10_000_000, "число уточнило новую ветку"
    scooter = current[min(roots)]
    assert scooter.passport.budget.max is None, "число не уехало в соседний поиск"
    assert scooter.version == 1, "а скутер не тронут"


async def test_the_loser_of_the_race_is_an_ordinary_message_not_a_lost_one() -> None:
    """Проигравший `/new` не теряет сообщение: оно идёт обычным путём в открытую ветку."""
    store = MemoryStore()
    talker = _talk_slowly(store, {"до 10 млн донгов": 0.05})
    await talker.on_text(CLIENT, "ищу скутер в нячанге", Replies())
    await talker.start_new(CLIENT)
    replies = Replies()

    await asyncio.gather(
        talker.on_text(CLIENT, "сниму квартиру в нячанге", Replies()),
        talker.on_text(CLIENT, "до 10 млн донгов", replies),
    )

    assert replies.texts, "проигравшее сообщение получило ответ, а не тишину"
    assert (await store.load(CLIENT)).starting_new is False, "флаг потрачен, а не оставлен"


async def test_a_thread_that_lost_the_flag_race_is_never_created() -> None:
    """Хранилище отказывает проигравшему: `start_requested` на потраченном флаге — `None`."""
    store = MemoryStore()
    await talk(store).on_text(CLIENT, "ищу скутер в нячанге", Replies())
    await store.await_new(await store.load(CLIENT))
    armed = await store.load(CLIENT)
    assert armed.starting_new

    first = await store.start_requested(armed, bike(raw_query="сниму квартиру"))
    second = await store.start_requested(armed, bike(raw_query="до 10 млн"))

    assert first is not None
    assert second is None, "один `/new` — одна ветка"
    assert len({row.root for row in store.rows}) == 2


# ── метрика разбора на пути /new ────────────────────────────────────────────


class _Journal:
    """Журнал, который помнит этапы закрытых ходов."""

    def __init__(self) -> None:
        self.stages: list[dict[str, int]] = []

    async def open_request(self, tg_user_id: int, text: str, *, username: str | None = None) -> Any:
        return None

    async def log_answer(self, opened: Any, text: str) -> None:
        return None

    async def close_request(self, opened: Any, *, stages: dict[str, int], **_kwargs: Any) -> None:
        self.stages.append(dict(stages))


async def test_a_turn_through_new_still_reports_its_intake_time() -> None:
    """Разбор на пути `/new` стоит столько же, и дашборд обязан его видеть.

    Без отметки `intake_ms` у запросов через `/new` в дашборде не было бы разбора,
    и доля времени на него поехала бы вниз на ровном месте.
    """
    journal = _Journal()
    talker = Conversation(MemoryStore(), intake=lambda: Rules(), finder=nothing, recorder=journal)
    await talker.on_text(CLIENT, "ищу скутер в нячанге", Replies())
    await talker.start_new(CLIENT)

    await talker.on_text(CLIENT, "сниму квартиру в нячанге", Replies())

    assert "intake_ms" in journal.stages[-1]
