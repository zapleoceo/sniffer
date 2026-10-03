"""Подборка из нескольких карточек: сообщение не длиннее 4096 знаков, карточка не разорвана.

Раньше подборка из девяти карточек (≈ 460 знаков каждая) уходила одним сообщением,
получала 400 «message is too long» и пропадала целиком. Теперь она режется по
границе карточки, нумеруется и не превышает лимита карточек в одном сообщении.
"""

from __future__ import annotations

import random
import re
from html.parser import HTMLParser
from itertools import pairwise
from typing import Any

import pytest

from sniffer.notifier.delivery import render
from sniffer.notifier.digest import (
    CARDS_PER_MESSAGE,
    MESSAGE_LIMIT,
    SEPARATOR,
    header,
    split,
)
from tests.notifier_support import Boom, Clock, Row, Store, deliver, digest_row

LONG_SUMMARY = "я" * 300


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instantly(_seconds: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", instantly)


# ── разбиение: чистая функция ───────────────────────────────────────────────


def test_a_digest_that_fits_stays_in_one_message() -> None:
    assert split(["a" * 100] * 3) == [[0, 1, 2]]


def test_the_card_limit_applies_even_when_the_text_would_fit() -> None:
    groups = split(["карточка"] * 20)

    assert [len(group) for group in groups] == [CARDS_PER_MESSAGE, CARDS_PER_MESSAGE, 4]


def test_nothing_to_split_gives_no_messages() -> None:
    assert split([]) == []


def test_a_card_longer_than_the_limit_gets_a_message_of_its_own() -> None:
    cards = ["a" * 100, "b" * 5000, "c" * 100, "d" * 100]

    assert split(cards) == [[0], [1], [2, 3]]


def test_the_room_for_the_header_is_taken_for_the_longest_number() -> None:
    """Сколько будет сообщений, известно после разбиения, а заголовок входит в длину."""
    cards = ["x" * 1000] * 4  # четыре по 1000: влезают в 4096 без заголовка, с ним — три

    (first, *_) = split(cards)

    longest = len(header(99, 99)) + len(SEPARATOR) + len(SEPARATOR.join(cards[: len(first)]))
    assert longest <= MESSAGE_LIMIT


def test_a_message_filled_exactly_to_the_limit_is_allowed_one_more_character_is_not() -> None:
    """Граница включена: ровно 4096 знаков Telegram принимает, 4097 — уже нет."""
    room = MESSAGE_LIMIT - len(header(99, 99)) - len(SEPARATOR)
    first = room // 2
    exact = ["a" * first, "b" * (room - first - len(SEPARATOR))]
    over = ["a" * first, "b" * (room - first - len(SEPARATOR) + 1)]

    assert split(exact) == [[0, 1]]
    assert split(over) == [[0], [1]]


def test_splitting_is_exact_on_random_cards() -> None:
    """Каждая карточка в одном сообщении ровно раз, порядок цел, лимиты соблюдены, лишнего нет."""
    rng = random.Random(20261003)  # noqa: S311 — детерминированная выборка, не секрет
    for _ in range(300):
        cards = ["x" * rng.randint(1, 1500) for _ in range(rng.randint(1, 40))]

        groups = split(cards)

        assert [i for group in groups for i in group] == list(range(len(cards)))
        for group in groups:
            joined = len(
                header(len(groups), len(groups))
                + SEPARATOR
                + SEPARATOR.join(cards[i] for i in group)
            )
            assert len(group) <= CARDS_PER_MESSAGE
            assert len(group) == 1 or joined <= MESSAGE_LIMIT
        for left, right in pairwise(groups):
            grown = [*left, right[0]]
            text = header(99, 99) + SEPARATOR + SEPARATOR.join(cards[i] for i in grown)
            assert len(grown) > CARDS_PER_MESSAGE or len(text) > MESSAGE_LIMIT, (
                "сообщение разрезано зря: следующая карточка ещё помещалась"
            )


# ── подборка в очереди ──────────────────────────────────────────────────────


def cards(count: int, **payload: Any) -> list[Row]:
    """Подборка одного клиента: `count` карточек по ~400 знаков каждая."""
    fields = {"summary": LONG_SUMMARY, **payload}
    return [digest_row(n, **fields) for n in range(1, count + 1)]


def numbers(text: str) -> list[int]:
    return [int(n) for n in re.findall(r"Карточка (\d+)", text)]


class Tags(HTMLParser):
    """Разметка подборки: только `b` и `a`, и все закрыты — иначе Bot API ответит 400."""

    def __init__(self) -> None:
        super().__init__()
        self.open: list[str] = []
        self.seen: set[str] = set()

    def handle_starttag(self, tag: str, attrs: object) -> None:
        self.open.append(tag)
        self.seen.add(tag)

    def handle_endtag(self, tag: str) -> None:
        assert self.open and self.open.pop() == tag, f"тег </{tag}> закрыт не там"


def assert_well_formed(text: str) -> None:
    parser = Tags()
    parser.feed(text)
    parser.close()
    assert not parser.open and parser.seen <= {"b", "a"}, (parser.open, parser.seen)


async def test_a_long_digest_is_split_under_4096_and_keeps_every_card_in_order() -> None:
    store, clock = Store(cards(20)), Clock()
    delivery, telegram = deliver(store, clock)

    assert await delivery.tick() == 20

    assert len(telegram.texts) == 3
    assert all(len(text) <= MESSAGE_LIMIT for text in telegram.texts), "один из кусков не пролез"
    assert [n for text in telegram.texts for n in numbers(text)] == list(range(1, 21))
    assert all(len(numbers(text)) <= CARDS_PER_MESSAGE for text in telegram.texts)
    assert all(store.row(n).status == "sent" for n in range(1, 21))


async def test_a_digest_longer_than_one_batch_is_finished_by_the_next_pass() -> None:
    """За проход берётся не больше BATCH строк: остаток уходит следующей подборкой, не теряется."""
    store, clock = Store(cards(30)), Clock()
    delivery, telegram = deliver(store, clock)

    assert (await delivery.tick(), await delivery.tick()) == (20, 10)

    assert [n for text in telegram.texts for n in numbers(text)] == list(range(1, 31))
    assert all(len(text) <= MESSAGE_LIMIT for text in telegram.texts)


async def test_the_parts_of_one_digest_are_numbered() -> None:
    store, clock = Store(cards(20)), Clock()
    delivery, telegram = deliver(store, clock)

    await delivery.tick()

    count = len(telegram.texts)
    assert count == 3
    for number, text in enumerate(telegram.texts, start=1):
        first_line = text.splitlines()[0]
        # Ожидание записано буквами, а не вызовом `header`: иначе тест повторял бы код.
        assert first_line == f"<b>Новые находки по вашему запросу</b> ({number} из {count})"


async def test_a_digest_in_one_message_has_no_number() -> None:
    store, clock = Store(cards(3)), Clock()
    delivery, telegram = deliver(store, clock)

    await delivery.tick()

    assert len(telegram.texts) == 1
    assert telegram.texts[0].splitlines()[0] == "<b>Новые находки по вашему запросу</b>"


async def test_a_card_left_over_by_the_split_still_goes_as_a_part_of_the_digest() -> None:
    store, clock = Store(cards(CARDS_PER_MESSAGE + 1)), Clock()
    delivery, telegram = deliver(store, clock)

    await delivery.tick()

    assert len(telegram.texts) == 2
    assert "(2 из 2)" in telegram.texts[1].splitlines()[0], "одинокая карточка потеряла заголовок"


async def test_every_part_is_confirmed_in_its_own_transaction() -> None:
    store, clock = Store(cards(CARDS_PER_MESSAGE + 2)), Clock()
    delivery, _ = deliver(store, clock)

    await delivery.tick()

    first = ",".join(str(n) for n in range(1, CARDS_PER_MESSAGE + 1))
    second = ",".join(str(n) for n in range(CARDS_PER_MESSAGE + 1, CARDS_PER_MESSAGE + 3))
    locks = [event for event in store.events if event.startswith("lock:")]
    assert locks == [f"lock:{first}", f"lock:{second}"]
    assert store.events.count("commit") == 3, "уборка и по коммиту на каждую часть"


async def test_a_failure_of_one_part_leaves_the_other_parts_alone() -> None:
    store, clock = Store(cards(20)), Clock()
    delivery, telegram = deliver(store, clock, None, Boom("сеть"), None)

    assert await delivery.tick() == 12

    statuses = [store.row(n).status for n in (1, 8, 9, 16, 17, 20)]
    assert statuses == ["sent", "sent", "pending", "pending", "sent", "sent"]
    assert store.row(9).attempts == 1 and store.row(1).attempts == 0
    assert len(telegram.texts) == 3


async def test_digests_of_two_clients_are_never_mixed() -> None:
    rows = [digest_row(n, user_id=7) for n in (1, 2)] + [digest_row(n, user_id=8) for n in (3, 4)]
    store, clock = Store(rows), Clock()
    delivery, telegram = deliver(store, clock)

    await delivery.tick()

    assert telegram.recipients == [42, 43]
    assert [numbers(text) for text in telegram.texts] == [[1, 2], [3, 4]]


async def test_an_oversized_card_gets_a_message_of_its_own_and_spares_its_neighbours() -> None:
    rows = [digest_row(n) for n in (1, 2, 3)]
    rows.append(digest_row(4, url="https://t.me/" + "x" * 4500))
    rows += [digest_row(n) for n in (5, 6)]
    store, clock = Store(rows), Clock()
    delivery, telegram = deliver(store, clock)

    await delivery.tick()

    assert [numbers(text) for text in telegram.texts] == [[1, 2, 3], [4], [5, 6]]
    assert all(store.row(n).status == "sent" for n in range(1, 7))


# ── экранирование: контракт `escape` ────────────────────────────────────────

HOSTILE = '<script>alert("x")</script> & <b>Honda</b> "q" \'s\''


async def test_hostile_text_is_escaped_in_every_part_of_a_split_digest() -> None:
    rows = cards(20, summary=HOSTILE, price_display="<1 млн & &amp;")
    rows = [Row(r.id, r.user_id, r.recipient_id, {**r.payload, "title": HOSTILE}) for r in rows]
    store, clock = Store(rows), Clock()
    delivery, telegram = deliver(store, clock)

    await delivery.tick()

    assert len(telegram.texts) == 3
    for text in telegram.texts:
        assert "<script>" not in text and "<b>Honda</b>" not in text, "чужая разметка прошла"
        assert_well_formed(text)
    assert sum(text.count("&lt;script&gt;") for text in telegram.texts) == 20 * 2


def test_a_title_is_clipped_before_escaping_so_an_entity_is_never_cut() -> None:
    card = render({"title": "&" * 1000, "url": "https://t.me/c/1/1"})

    assert "&amp;" * 199 + "…" in card
    assert not re.search(r"&(?!amp;)", card), "сущность разрезана пополам"


HOSTILE_PAYLOADS = [
    {},
    {"title": None, "summary": None, "url": None, "price_amount": None},
    {"title": 0, "summary": 1.5, "url": ["не", "строка"], "price_amount": {"x": 1}},
    {
        "title": "x" * 10_000,
        "summary": "y" * 10_000,
        "price_amount": "²",
        "price_currency": "<VND>",
    },
    {"price_amount": "١٢٣", "price_currency": "\u202e"},
    {"price_amount": "-5.5", "price_display": "   "},
    {"kind": "collection_result", "items": "не список", "intro": None},
    {"kind": "collection_result", "items": [1, None, {"title": "<i>"}], "intro": "<b>"},
]


@pytest.mark.parametrize("payload", HOSTILE_PAYLOADS, ids=range(len(HOSTILE_PAYLOADS)))
def test_rendering_never_raises_on_hostile_data_and_never_lets_markup_through(
    payload: dict[str, object],
) -> None:
    """Планирование подборки собирает тексты заранее: бросок здесь уронил бы весь проход."""
    text = render(payload)

    assert isinstance(text, str)
    for tag in re.findall(r"<(/?\w+)", text):
        assert tag.lstrip("/") in {"b", "a"}, f"чужой тег <{tag}>"
