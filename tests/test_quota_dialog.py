"""Диалог с квотой: показ через допуск, остаток, исчерпание, возврат при сбое.

Настоящий `Conversation` на подделках: хранилище разговора (`MemoryStore`), журнал
показов (`MemoryLedger`), журнал запросов (`FakeJournal`). База и Telegram не нужны:
контракт самого журнала проверен на подделке и на Postgres отдельно
(`test_quota_ledger.py`), здесь — что с ним делает ход диалога.
"""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from structlog.testing import capture_logs

from sniffer.bot import journal, wording_plan
from sniffer.bot.conversation import Conversation, Found, Reply
from sniffer.bot.quota import QuotaService
from sniffer.bot.store import Client
from sniffer.domain.passport import Passport
from sniffer.domain.plans import FREE_CARDS_PER_PERIOD
from sniffer.domain.quota import Channel
from sniffer.search.intake_rules import parse_query
from sniffer.simulation.ledger import Counters, MemoryLedger
from sniffer.simulation.stubs import MemoryStore
from sniffer.sources.base import RawItem
from tests.quota_support import Clock

T0 = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)
CLIENT = Client(tg_user_id=42, username="dima")
OWNER = Client(tg_user_id=169510539, username="boss")
SEARCH = "скутер в Нячанге"
FRESH = datetime.now(UTC) - timedelta(days=1)


pytestmark = pytest.mark.usefixtures("sales_on")


class Rules:
    async def parse(self, text: str) -> Passport:
        return parse_query(text)


class Replies:
    def __init__(self) -> None:
        self.sent: list[Reply] = []

    async def __call__(self, reply: Reply) -> None:
        self.sent.append(reply)

    @property
    def texts(self) -> list[str]:
        return [reply.text for reply in self.sent]


class Journal:
    """Журнал запросов без базы: отдаёт номер запроса, как настоящий."""

    def __init__(self) -> None:
        self.count = 0

    async def open_request(
        self, tg_user_id: int, text: str, *, username: str | None = None
    ) -> journal.OpenRequest:
        self.count += 1
        return journal.OpenRequest(user_id=1, request_id=self.count)

    async def log_answer(self, opened: journal.OpenRequest | None, text: str) -> None:
        return None

    async def close_request(self, opened: journal.OpenRequest | None, **kwargs: Any) -> None:
        return None


def card(number: int, *, listing: bool = True, source: str = "archive") -> RawItem:
    return RawItem(
        source=source,
        external_id=f"ext-{number}",
        url=f"https://t.me/c/1/{number}",
        title=f"Honda Vision {number}",
        price_raw="25.000.000 đ",
        posted_at=FRESH,
        raw={"listing_id": number} if listing else {},
    )


def cards(first: int, count: int) -> list[RawItem]:
    return [card(number) for number in range(first, first + count)]


class World:
    """Разговор, журнал и «найденное»: что вернёт поиск, меняется между ходами."""

    def __init__(self, *, clock: Clock | None = None, ledger: Any = None) -> None:
        self.ledger = ledger or MemoryLedger()
        self.clock = clock or Clock(T0)
        self.quota = QuotaService(self.ledger, clock=self.clock, owner_tg_id=OWNER.tg_user_id)
        self.found: list[RawItem] = []
        self.talk = Conversation(
            MemoryStore(),
            intake=lambda: Rules(),
            finder=self._finder,
            recorder=Journal(),
            quota=self.quota,
        )

    async def _finder(self, _passport: Passport) -> Found:
        return Found(items=list(self.found))

    async def search(
        self, items: list[RawItem], client: Client = CLIENT, send: Any = None
    ) -> Replies:
        self.found = items
        replies = send or Replies()
        await self.talk.on_text(client, SEARCH, replies)
        return cast(Replies, replies)

    async def again(
        self, items: list[RawItem], client: Client = CLIENT, send: Any = None
    ) -> Replies:
        """«Искать снова»: тот же поиск, другой результат."""
        self.found = items
        replies = send or Replies()
        await self.talk.repeat(client, 1, replies)
        return cast(Replies, replies)


def results_of(replies: Replies) -> Reply:
    """Сообщение с выдачей: первое — «Понял», дальше то, что решила квота."""
    return replies.sent[1]


def shown_numbers(reply: Reply) -> int:
    return reply.text.count("открыть оригинал")


# ── остаток над выдачей ─────────────────────────────────────────────────────


async def test_the_first_search_shows_a_page_and_says_how_many_free_cards_are_left() -> None:
    world = World()

    replies = await world.search(cards(1, 10))

    assert len(replies.sent) == 2, "«Понял» и выдача: предложения пока нет, лимит цел"
    shown = results_of(replies)
    assert shown_numbers(shown) == 5, "страница прежняя: пять карточек из десяти найденных"
    assert "Бесплатно осталось 5 из 10 до 17 ноября." in shown.text
    rows = world.ledger.rows(1)
    assert len(rows) == 5 and all(view.delivered_at is not None for view in rows)
    assert {view.passport_root for view in rows} == {1}, "ветка поиска записана в журнал"
    assert {view.request_id for view in rows} == {1}
    assert world.ledger.offered == {}, "пока лимит цел, право предложить подписку не тратится"


async def test_the_balance_date_is_the_vietnamese_one_even_when_utc_is_a_day_earlier() -> None:
    """Якорь 16 октября 18:00 UTC — это 17-е по Хошимину; обновление «17 ноября»."""
    world = World(clock=Clock(datetime(2026, 10, 16, 18, 0, tzinfo=UTC)))

    replies = await world.search(cards(1, 3))

    assert "до 17 ноября." in results_of(replies).text


async def test_two_pages_spend_the_free_ten_and_the_third_search_meets_the_offer() -> None:
    world = World()
    await world.search(cards(1, 5))
    second = await world.again(cards(6, 5))
    third = await world.again(cards(11, 5))

    assert "Бесплатно осталось 0 из 10" in results_of(second).text
    assert len(third.sent) == 2, "«Понял» и одно сообщение: карточек нет, оно и есть предложение"
    offer = third.sent[1]
    assert offer.text == wording_plan.exhausted_offer(
        total=5, renews=datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
    )
    assert offer.offer_plan is True and shown_numbers(offer) == 0
    assert len(world.ledger.rows(1)) == FREE_CARDS_PER_PERIOD, "за лимит ничего не записано"


async def test_the_offer_is_not_repeated_within_a_day_and_comes_back_after_one() -> None:
    world = World()
    await world.search(cards(1, 5))
    await world.again(cards(6, 5))
    first = await world.again(cards(11, 5))
    same_day = await world.again(cards(16, 5))
    world.clock.tick(timedelta(hours=24, minutes=1))
    next_day = await world.again(cards(21, 5))

    assert first.sent[1].offer_plan is True
    assert same_day.sent[1].offer_plan is False, "второй раз за сутки — короткий ответ без кнопки"
    assert same_day.sent[1].text == wording_plan.exhausted_short(
        total=5, renews=datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
    )
    assert next_day.sent[1].offer_plan is True, "через сутки предложение снова уместно"


async def test_searching_again_with_cards_already_seen_costs_nothing() -> None:
    world = World()
    same = cards(1, 5)

    await world.search(same)
    await world.again(same)
    third = await world.again(same)

    assert "Бесплатно осталось 5 из 10" in results_of(third).text
    assert [view.times_shown for view in world.ledger.rows(1)] == [3] * 5


async def test_what_was_seen_stays_available_after_the_free_ten_are_spent() -> None:
    world = World()
    seen = cards(1, 5)
    await world.search(seen)
    await world.again(cards(6, 5))

    again = await world.again(seen + cards(11, 5))

    assert shown_numbers(results_of(again)) == 5, "оплаченное не прячется за лимитом"
    assert "Бесплатно осталось 0 из 10" in results_of(again).text


# ── частичная выдача и предложение ──────────────────────────────────────────


async def spend(world: World, count: int, *, first: int = 100) -> None:
    """Списать часть квоты заранее — так, как это сделал бы прошлый показ."""
    from sniffer.bot.quota import Account

    who = Account(user_id=1, tg_user_id=CLIENT.tg_user_id)
    await world.quota.confirm(await world.quota.admit(who, list(range(first, first + count))))


async def test_a_partial_issue_shows_the_remainder_then_the_honest_rest_then_one_offer() -> None:
    world = World()
    await spend(world, 8)
    # Первый вызов `admit` поставил якорь и период; поиск сейчас идёт в нём же.

    replies = await world.search(cards(1, 5))

    assert [reply.offer_plan for reply in replies.sent] == [False, False, True]
    shown, offer = results_of(replies), replies.sent[2]
    assert shown_numbers(shown) == 2, "осталось две бесплатные из десяти"
    assert "Бесплатно осталось 0 из 10 до 17 ноября." in shown.text
    assert "Показываю 2" in shown.text
    assert shown.text.endswith(
        wording_plan.more_line(
            3, limit=10, renews=datetime(2026, 11, 17, 9, 30, tzinfo=UTC), selling=True
        )
    )
    assert offer.text == wording_plan.exhausted_offer(
        total=None, renews=datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
    )


async def test_the_offer_goes_after_the_cards_and_the_cards_are_already_confirmed() -> None:
    """Предложение не задерживает карточки и не теряет их при своём сбое."""
    world = World()
    await spend(world, 8)
    seen_at_offer: list[bool] = []

    async def send(reply: Reply) -> None:
        if reply.offer_plan:
            rows = [view for view in world.ledger.rows(1) if view.listing_id < 100]
            seen_at_offer.append(bool(rows) and all(view.delivered_at for view in rows))

    await world.search(cards(1, 5), send=send)

    assert seen_at_offer == [True]


# ── владелец ────────────────────────────────────────────────────────────────


async def test_the_owner_has_no_limit_no_balance_line_and_no_offer() -> None:
    world = World()

    first = await world.search(cards(1, 5), client=OWNER)
    second = await world.again(cards(6, 5), client=OWNER)
    third = await world.again(cards(11, 5), client=OWNER)

    for replies in (first, second, third):
        assert len(replies.sent) == 2 and shown_numbers(results_of(replies)) == 5
        assert "осталось" not in results_of(replies).text.lower()
    assert len(world.ledger.rows(1)) == 15, (
        "журнал для владельца пишется: «уже показано» нужно всем"
    )


# ── сбои ────────────────────────────────────────────────────────────────────


async def test_a_failed_send_returns_the_slots_so_a_retry_gets_the_same_cards() -> None:
    world = World()

    async def broken(reply: Reply) -> None:
        if "открыть оригинал" in reply.text:
            raise ConnectionError("Telegram не принял сообщение")

    with pytest.raises(ConnectionError):
        await world.search(cards(1, 5), send=broken)
    assert world.ledger.rows(1) == [], "слоты вернулись: человек карточек не видел"

    retry = await world.again(cards(1, 5))
    assert shown_numbers(results_of(retry)) == 5
    assert "Бесплатно осталось 5 из 10" in results_of(retry).text


async def test_a_cancelled_send_also_returns_the_slots() -> None:
    """Отмена по таймауту — тоже «не показано»: возврат идёт из корня иерархии исключений."""
    import asyncio

    world = World()

    async def cancelled(reply: Reply) -> None:
        if "открыть оригинал" in reply.text:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await world.search(cards(1, 5), send=cancelled)

    assert world.ledger.rows(1) == []


class BrokenLedger(MemoryLedger):
    def __init__(self, *, reserve: bool = False, confirm: bool = False) -> None:
        super().__init__()
        self.break_reserve, self.break_confirm = reserve, confirm

    async def reserve(self, claim: Any) -> Any:
        if self.break_reserve:
            raise ConnectionError("база недоступна")
        return await super().reserve(claim)

    async def confirm(self, ticket: Any, at: Any) -> None:
        if self.break_confirm:
            raise ConnectionError("база недоступна")
        await super().confirm(ticket, at)


async def test_when_the_quota_cannot_be_read_the_person_gets_an_honest_reply_not_free_cards() -> (
    None
):
    world = World(ledger=BrokenLedger(reserve=True))

    replies = await world.search(cards(1, 5))

    assert replies.texts[-1] == wording_plan.QUOTA_UNAVAILABLE
    assert all("открыть оригинал" not in text for text in replies.texts)


async def test_a_failed_confirmation_does_not_take_the_cards_away_from_the_person() -> None:
    world = World(ledger=BrokenLedger(confirm=True))

    with capture_logs() as logs:
        replies = await world.search(cards(1, 5))

    assert shown_numbers(results_of(replies)) == 5
    assert any(entry["event"] == "quota.confirm_failed" for entry in logs)


# ── что считается карточкой выдачи ──────────────────────────────────────────


async def test_the_quota_meets_only_the_page_not_everything_that_was_found() -> None:
    world = World()

    replies = await world.search(cards(1, 40))

    assert shown_numbers(results_of(replies)) == 5
    assert len(world.ledger.rows(1)) == 5, "остальные 35 найдены, но не показаны и не списаны"


async def test_a_card_the_journal_cannot_identify_is_shown_uncounted_and_the_log_says_so() -> None:
    world = World()
    stray = [card(number, listing=False, source="chotot") for number in range(1, 4)]

    with capture_logs() as logs:
        replies = await world.search(stray)

    assert shown_numbers(results_of(replies)) == 3, "выдача не прячется молча"
    assert world.ledger.rows(1) == []
    warned = [entry for entry in logs if entry["event"] == "quota.unidentified_cards"]
    assert warned and warned[0]["count"] == 3 and warned[0]["sources"] == ["chotot"]


async def test_a_card_found_live_is_identified_by_source_and_external_id() -> None:
    world = World()
    live = [card(number, listing=False, source="chotot") for number in range(1, 4)]
    world.ledger.known.update({("chotot", f"ext-{number}"): 500 + number for number in range(1, 4)})

    replies = await world.search(live)

    assert [view.listing_id for view in world.ledger.rows(1)] == [501, 502, 503]
    assert "Бесплатно осталось 7 из 10" in results_of(replies).text


async def test_a_search_that_finds_nothing_neither_spends_nor_starts_the_period() -> None:
    world = World()

    replies = await world.search([])

    assert replies.texts[-1].startswith("По этому запросу ничего не нашлось")
    assert world.ledger.anchors == {} and world.ledger.rows(1) == []


async def test_the_request_counters_follow_what_was_shown_and_what_the_limit_held_back() -> None:
    world = World()
    await spend(world, 8)

    await world.search(cards(1, 5))

    assert world.ledger.counters[1] == Counters(shown=2, withheld=3)


async def test_without_a_quota_the_dialog_is_exactly_as_it_was() -> None:
    talk = Conversation(
        MemoryStore(),
        intake=lambda: Rules(),
        finder=lambda _passport: _found(cards(1, 8)),
        recorder=Journal(),
    )
    replies = Replies()

    await talk.on_text(CLIENT, SEARCH, replies)

    text = replies.texts[-1]
    assert "осталось" not in text.lower() and text.count("открыть оригинал") == 5


def _found(items: list[RawItem]) -> Awaitable[Found]:
    async def done() -> Found:
        return Found(items=items)

    return done()


async def test_the_monitor_channel_is_never_charged_by_the_dialog() -> None:
    """Слежение и поиск делят журнал, но потолок тратит только поиск."""
    from sniffer.bot.quota import Account

    world = World()
    who = Account(user_id=1, tg_user_id=CLIENT.tg_user_id)
    await world.quota.admit(who, list(range(900, 920)), Channel.MONITOR)

    replies = await world.search(cards(1, 5))

    assert "Бесплатно осталось 5 из 10" in results_of(replies).text
