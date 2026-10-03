"""Сквозные сценарии человека: апдейт Telegram → настоящий диспетчер → ответы Bot API.

Что подделано, см. `tests/e2e_support.py`. Тесты читаются как сценарии из
`docs/monetization.md`: ожидание — из документа, а не из текущего поведения кода.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerPreCheckoutQuery,
    CreateInvoiceLink,
    RefundStarPayment,
    SendMessage,
)
from aiogram.types import ReplyKeyboardMarkup

from sniffer.bot import billing_wording as words
from sniffer.bot import paging, threads, watch_flow, wording, wording_plan
from sniffer.bot import voice as voice_input
from sniffer.bot.billing import InvoicePayload
from sniffer.bot.quota import Account
from sniffer.bot.store import Client
from sniffer.config import reload_settings
from sniffer.domain import plans
from sniffer.domain.dialogue import CURRENCY_ASK
from sniffer.notifier.delivery import LOST_NOTE
from sniffer.sources.base import RawItem
from sniffer.worker.monitor import MonitorAgent
from sniffer.worker.monitor_scope import OVERFLOW_DELAY
from tests import billing_support as fx
from tests import e2e_support as e2e
from tests import monitor_support as mon
from tests.e2e_support import Flow
from tests.notifier_support import Row
from tests.test_tabs_notifier import GONE, FakeTabs, Wire, build


def _sales(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    """`Settings.selling` читают слова и кнопки бота; включаем продажу, как владелец."""
    monkeypatch.setenv("SALES_ENABLED", "true" if on else "false")
    monkeypatch.setenv("OWNER_CHAT_ID", "9001")
    reload_settings()


@pytest.fixture
def flow(monkeypatch: pytest.MonkeyPatch) -> Iterator[Flow]:
    _sales(monkeypatch, on=True)
    yield from e2e.build(monkeypatch)
    monkeypatch.undo()
    reload_settings()


@pytest.fixture
def closed_flow(monkeypatch: pytest.MonkeyPatch) -> Iterator[Flow]:
    """Продажа выключена: бот не зовёт покупать, а называет дату обновления лимита."""
    _sales(monkeypatch, on=False)
    yield from e2e.build(monkeypatch)
    monkeypatch.undo()
    reload_settings()


# ── 1. /start и кнопки меню ─────────────────────────────────────────────────


async def test_start_greets_with_the_rule_and_shows_the_five_buttons(flow: Flow) -> None:
    calls = await flow.command("/start")

    (sent,) = e2e.messages(calls)
    assert sent.text == wording.GREETING
    assert isinstance(sent.reply_markup, ReplyKeyboardMarkup)
    labels = [b.text for row in sent.reply_markup.keyboard for b in row]
    assert labels == list(wording.MENU_BUTTONS)


async def test_pressing_a_menu_button_does_not_reach_the_dialogue(flow: Flow) -> None:
    flow.found = e2e.lots(1, 3)
    for label in (
        wording.BTN_PLAN,
        wording.BTN_HELP,
        wording.BTN_REQUESTS,
        wording.BTN_SUBSCRIPTION,
    ):
        calls = await flow.say(label)
        assert e2e.messages(calls), f"кнопка «{label}» осталась без ответа"
    assert flow.store.rows == [], "ни одна кнопка не открыла поиск"
    assert flow.ledger.rows(1) == []


# ── 2. широкий запрос → вопросы со счётчиками → выдача ──────────────────────


def wide_market() -> list[RawItem]:
    return (
        e2e.lots(1, 12, brand="honda", model="lead")
        + e2e.lots(13, 8, brand="honda", model="vision")
        + e2e.lots(21, 10, brand="yamaha", model="exciter")
    )


async def test_a_wide_request_asks_with_counts_then_any_leads_to_the_top_ten(flow: Flow) -> None:
    flow.found = wide_market()

    calls = await flow.say("ищу скутер в Нячанге")

    first = texts_with_question(calls)
    assert first is not None, f"нет вопроса со счётчиком: {e2e.texts(calls)}"
    assert re.search(r"Подходит 30", first.text or "")
    assert "открыть оригинал" not in (first.text or ""), "до вопроса карточек нет"
    any_data = e2e.button(calls, "Любой")
    calls = await flow.tap(any_data)
    shown = " ".join(e2e.texts(calls))
    assert shown.count("открыть оригинал") <= 10


def texts_with_question(calls: list[object]) -> SendMessage | None:
    for call in e2e.messages(calls):
        if any("Показать все" in text for text, _, _ in e2e.buttons(call)):
            return call
    return None


async def settle(flow: Flow, text: str) -> list[object]:
    """Ответить «Любой» на каждый вопрос, пока бот не покажет карточки."""
    calls = await flow.say(text)
    for _ in range(6):
        if "открыть оригинал" in " ".join(e2e.texts(calls)):
            return calls
        calls = await flow.tap(e2e.button(calls, "Любой"))
    raise AssertionError(f"выдачи так и не было: {e2e.texts(calls)}")


async def test_the_first_page_has_five_cards_the_free_balance_and_a_remainder_button(
    flow: Flow,
) -> None:
    flow.found = wide_market()

    calls = await settle(flow, "ищу скутер в Нячанге")

    page = " ".join(e2e.texts(calls))
    assert page.count("открыть оригинал") == 5
    assert "Бесплатно осталось 5 из 10 до 17 ноября." in page
    assert e2e.button(calls, "Ещё 5").startswith("pg:")
    assert e2e.button(calls, "Показать все 25").startswith("pg:")
    assert (await flow.command("/plan"))[0].text.startswith("Бесплатно: использовано 5 из 10")


async def test_show_all_on_a_question_skips_narrowing_and_the_second_show_all_stops_at_ten(
    flow: Flow,
) -> None:
    flow.found = wide_market()
    calls = await flow.say("ищу скутер в Нячанге")

    calls = await flow.tap(e2e.button(calls, "Показать все 30"))

    first = " ".join(e2e.texts(calls))
    assert first.count("открыть оригинал") == 5, "«Показать все» на вопросе — первая страница"
    calls = await flow.tap(e2e.button(calls, "Показать все 25"))
    page = " ".join(e2e.texts(calls))
    assert page.count("открыть оригинал") == 5, "бесплатный потолок — десять карточек на период"
    assert "Ещё 20 подходящих вариантов — по подписке 10 ⭐/мес." in page
    plan = await flow.command("/plan")
    assert plan[0].text.startswith("Бесплатно: использовано 10 из 10")


async def test_more_pages_through_the_same_gate_and_the_offer_comes_once_a_day(
    flow: Flow,
) -> None:
    flow.found = wide_market()
    calls = await settle(flow, "ищу скутер в Нячанге")

    more = await flow.tap(e2e.button(calls, "Ещё 5"))

    page = " ".join(e2e.texts(more))
    assert "Карточки 6–10 из 30" in page and page.count("открыть оригинал") == 5
    assert "Бесплатно осталось 0 из 10" in page
    assert "Ещё 20 подходящих вариантов — по подписке 10 ⭐/мес." in page
    offers = [m for m in e2e.messages(more) if "Подписка" in str(e2e.buttons(m))]
    assert len(offers) <= 1


# ── 3. квота: повтор не списывает, исчерпание, /plan, смена периода ─────────


async def exhaust_free_quota(flow: Flow) -> list[object]:
    flow.found = wide_market()
    calls = await settle(flow, "ищу скутер в Нячанге")
    await flow.tap(e2e.button(calls, "Ещё 5"))
    return calls


async def test_a_repeated_search_shows_seen_cards_again_without_spending_the_quota(
    flow: Flow,
) -> None:
    await exhaust_free_quota(flow)

    again = await flow.tap("req:search:1")

    page = " ".join(e2e.texts(again))
    assert page.count("открыть оригинал") == 5, "виденные карточки показываются снова"
    assert "Бесплатно осталось 0 из 10" in page
    plan = await flow.command("/plan")
    assert str(plan[0].text).startswith("Бесплатно: использовано 10 из 10")


async def test_when_the_free_cards_are_over_the_offer_names_the_date_the_price_and_the_count(
    flow: Flow,
) -> None:
    await exhaust_free_quota(flow)
    flow.found = e2e.lots(100, 30, brand="honda", model="lead")

    calls = await flow.tap("req:search:1")

    offer = e2e.messages(calls)[-1]
    text = str(offer.text)
    assert "Бесплатные 10 карточек закончились — новые откроются 17 ноября." in text
    assert "Сейчас подходящих объявлений: 30" in text, "число бесплатно, бот не молчит"
    assert "10 ⭐ в месяц" in text and "до 300 карточек" in text
    assert "подождать до 17 ноября" in text, "вторая дорога без давления"
    assert "открыть оригинал" not in text
    label, data, _ = e2e.buttons(offer)[0]
    assert label == "Подписка — 10 ⭐/мес" and data == "plan:subscribe"


async def test_the_offer_is_not_repeated_within_a_day_but_the_date_is(flow: Flow) -> None:
    await exhaust_free_quota(flow)
    flow.found = e2e.lots(100, 30, brand="honda", model="lead")
    await flow.tap("req:search:1")

    second = e2e.messages(await flow.tap("req:search:1"))[-1]

    assert str(second.text).startswith("Бесплатные 10 карточек на этот период закончились")
    assert "17 ноября" in str(second.text)
    assert not any(data == "plan:subscribe" for _, data, _ in e2e.buttons(second))


async def test_the_subscribe_button_of_the_offer_leads_to_the_priced_consent_screen(
    flow: Flow,
) -> None:
    await exhaust_free_quota(flow)
    flow.found = e2e.lots(100, 30, brand="honda", model="lead")
    offer = await flow.tap("req:search:1")

    screen = await flow.tap(e2e.button(offer, "Подписка"))

    text = str(e2e.messages(screen)[-1].text)
    assert text.startswith("Подписка: 10 ⭐ в месяц, продлевается автоматически")
    labels = [label for label, _, _ in e2e.buttons(e2e.messages(screen)[-1])]
    assert labels == ["Принимаю условия, перейти к оплате", "Читать условия", "Не сейчас"]


async def test_with_sales_off_the_free_cards_end_in_a_fact_and_a_date_without_a_button(
    closed_flow: Flow,
) -> None:
    await exhaust_free_quota(closed_flow)
    closed_flow.found = e2e.lots(100, 30, brand="honda", model="lead")

    calls = await closed_flow.tap("req:search:1")

    last = e2e.messages(calls)[-1]
    text = str(last.text)
    assert text.startswith("Использовано 10 из 10 бесплатных карточек. Период обновится 17 ноября.")
    assert "Сейчас подходящих объявлений: 30" in text
    assert "подписк" not in text.lower() and "⭐" not in text
    assert not any(data == "plan:subscribe" for _, data, _ in e2e.buttons(last))


async def test_with_sales_off_the_remainder_line_does_not_offer_a_subscription(
    closed_flow: Flow,
) -> None:
    closed_flow.found = wide_market()
    calls = await settle(closed_flow, "ищу скутер в Нячанге")

    page = " ".join(e2e.texts(await closed_flow.tap(e2e.button(calls, "Ещё 5"))))

    assert "после обновления лимита 17 ноября" in page
    assert "по подписке" not in page
    assert not any("Подписка" in str(e2e.buttons(m)) for m in e2e.messages(calls))


async def test_a_new_period_returns_the_free_ten_and_plan_names_the_new_date(flow: Flow) -> None:
    from datetime import timedelta

    await exhaust_free_quota(flow)
    flow.clock.tick(timedelta(days=32))

    plan = await flow.command("/plan")
    assert str(plan[0].text).startswith("Бесплатно: использовано 0 из 10")
    assert "Обновится 17 декабря" in str(plan[0].text)
    page = " ".join(e2e.texts(await flow.tap("req:search:1")))
    assert "Бесплатно осталось 5 из 10 до 17 декабря." in page


async def test_plan_before_any_card_says_the_period_has_not_started_and_costs_nothing(
    flow: Flow,
) -> None:
    plan = await flow.command("/plan")

    assert "Он начнётся с первой выданной карточки" in str(plan[0].text)
    assert flow.ledger.rows(1) == [] and flow.store.rows == []


# ── 4. предел поисков: бесплатно 1, платно 10 ───────────────────────────────

REFUSED_FREE = (
    "У вас уже 1 поиск — поставьте на паузу или удалите один, "
    "либо оформите подписку: тогда можно держать до 10."
)
TOPICS = [
    "скутер в Нячанге",
    "скутер в Дананге",
    "квартира в Нячанге",
    "квартира в Дананге",
    "скутер в Хошимине",
    "квартира в Хошимине",
    "скутер в Ханое",
    "квартира в Ханое",
    "скутер в Далате",
    "квартира в Далате",
    "скутер в Хюэ",
]


async def test_a_free_account_gets_one_search_and_the_second_is_refused_in_words(
    flow: Flow,
) -> None:
    flow.found = e2e.lots(1, 3)
    await flow.say("ищу скутер в Нячанге")

    calls = await flow.say("ищу квартиру в Дананге")

    (refusal,) = e2e.messages(calls)
    assert refusal.text == REFUSED_FREE
    assert len({r.root for r in flow.store.rows}) == 1, "вторая ветка не родилась"


async def test_the_refusal_by_plain_text_carries_the_panel_button_like_the_other_paths(
    flow: Flow,
) -> None:
    flow.found = e2e.lots(1, 3)
    await flow.say("ищу скутер в Нячанге")

    (refusal,) = e2e.messages(await flow.say("ищу квартиру в Дананге"))

    assert [label for label, _, _ in e2e.buttons(refusal)] == ["🔔 Мои слежения"]


async def test_new_by_command_and_by_button_are_refused_before_asking_for_the_text(
    flow: Flow,
) -> None:
    flow.found = e2e.lots(1, 3)
    await flow.say("ищу скутер в Нячанге")

    by_command = e2e.messages(await flow.command("/new квартира в Дананге"))
    by_button = e2e.messages(await flow.say(wording.BTN_NEW))
    by_inline = e2e.messages(await flow.tap("req:new:0"))

    for refused in (by_command, by_button, by_inline):
        assert [m.text for m in refused] == [REFUSED_FREE], (
            "просить описать и отказать — худший порядок"
        )
        label, data, _ = e2e.buttons(refused[0])[0]
        assert label == "🔔 Мои слежения" and str(data).startswith("wch:")
        # Единственный поиск можно заменить прямо в отказе, а не только удалить через панель.
        assert [b[0] for b in e2e.buttons(refused[0])[1:]] == ["🔁 Заменить", "Оставить"]
    assert len({r.root for r in flow.store.rows}) == 1


async def test_a_paid_account_holds_ten_searches_and_the_eleventh_is_refused_without_a_pitch(
    flow: Flow,
) -> None:
    flow.paid.forced = 1
    flow.found = e2e.lots(1, 3)
    for topic in TOPICS[:10]:
        await flow.command("/new")
        await flow.say(f"ищу {topic}")
    assert len({r.root for r in flow.store.rows}) == 10

    calls = await flow.command(f"/new ищу {TOPICS[10]}")

    (refusal,) = e2e.messages(calls)
    assert refusal.text == "У вас уже 10 поисков — поставьте на паузу или удалите один."
    assert len({r.root for r in flow.store.rows}) == 10


# ── 5. оплата: счёт, pre_checkout, платёж, слот, потолок, возврат, продление ─

PAYLOAD = InvoicePayload(fx.CLIENT, words.TERMS_VERSION, fx.NONCE).encode()
MONTH = 30 * 86_400


def paid_until(flow: Flow, seconds: int = MONTH) -> int:
    return int(flow.clock().timestamp()) + seconds


async def pay(
    flow: Flow, *, charge: str = "charge-1", update_id: int = 3, **kw: object
) -> list[object]:
    update = fx.successful_payment(PAYLOAD, charge=charge, update_id=update_id, **kw)  # type: ignore[arg-type]
    return await flow.feed(update)


async def test_the_whole_purchase_from_the_screen_to_the_slot(flow: Flow) -> None:
    screen = await flow.command("/subscription")
    assert str(e2e.messages(screen)[-1].text).startswith("Подписка: 10 ⭐ в месяц")
    assert flow.session.sent(CreateInvoiceLink) == [], "до согласия ссылки нет"

    link = await flow.tap(e2e.button(screen, "Принимаю"))
    (invoice,) = [c for c in link if isinstance(c, CreateInvoiceLink)]
    assert (invoice.currency, invoice.subscription_period) == ("XTR", MONTH)
    assert [p.amount for p in invoice.prices] == [10] and invoice.payload == PAYLOAD
    pay_button = e2e.buttons(e2e.messages(link)[-1])[0]
    assert pay_button[2] == fx.LINK, "ссылка — кнопкой-адресом"

    checked = await flow.feed(fx.pre_checkout(PAYLOAD))
    (answer,) = [c for c in checked if isinstance(c, AnswerPreCheckoutQuery)]
    assert answer.ok is True

    assert await flow.paid.slots(Account(1, fx.CLIENT), flow.clock()) == 0
    done = await pay(flow, expiration=paid_until(flow))
    assert e2e.messages(done), "платёж отвечен"
    assert flow.slots.syncs == [fx.CLIENT], "слот пересчитан сразу после записи платежа"
    assert set(flow.bill.payments) == {"charge-1"}
    assert await flow.paid.slots(Account(1, fx.CLIENT), flow.clock()) == 1


async def test_a_foreign_amount_or_unknown_payload_is_declined_at_pre_checkout(flow: Flow) -> None:
    wrong_sum = await flow.feed(fx.pre_checkout(PAYLOAD, amount=1))
    garbage = await flow.feed(fx.pre_checkout("not-our-payload", update_id=9))

    for calls in (wrong_sum, garbage):
        (answer,) = [c for c in calls if isinstance(c, AnswerPreCheckoutQuery)]
        assert answer.ok is False and answer.error_message


async def test_sales_switched_off_answers_in_words_and_issues_no_link(flow: Flow) -> None:
    flow.sales_enabled = False

    calls = await flow.command("/subscription")
    tapped = await flow.tap(f"bill:accept:{words.TERMS_VERSION}")

    assert [m.text for m in e2e.messages(calls)] == [words.BILLING_OFF]
    assert e2e.buttons(e2e.messages(calls)[0]) == [], "кнопки согласия нет"
    assert flow.session.sent(CreateInvoiceLink) == [], (
        f"ссылка выдана при выключенных продажах: {tapped}"
    )
    assert e2e.messages(await flow.say(wording.BTN_SUBSCRIPTION))[0].text == words.BILLING_OFF


async def test_the_same_payment_update_twice_is_one_payment_and_one_reply(flow: Flow) -> None:
    await flow.tap(f"bill:accept:{words.TERMS_VERSION}")
    first = await pay(flow, expiration=paid_until(flow))
    again = await pay(flow, expiration=paid_until(flow))

    assert len(flow.bill.payments) == 1
    assert len(e2e.messages(first)) == 1
    assert e2e.messages(again) == [], "повтор того же платежа молчит, а не благодарит второй раз"


async def test_a_paid_account_is_capped_at_three_hundred_cards_and_is_not_sold_a_second_plan(
    flow: Flow,
) -> None:
    await flow.tap(f"bill:accept:{words.TERMS_VERSION}")
    await pay(flow, expiration=paid_until(flow))
    flow.found = wide_market()
    account = Account(user_id=flow.store._users.setdefault(fx.CLIENT, 1), tg_user_id=fx.CLIENT)
    seeded = await flow.quota.admit(account, list(range(1000, 1295)))
    await flow.quota.confirm(seeded)
    assert len(seeded.granted) == 295

    page = " ".join(e2e.texts(await settle(flow, "ищу скутер в Нячанге")))

    assert "Осталось 0 из 300 до 17 ноября" in page and "бесплатно" not in page.lower()
    assert page.count("открыть оригинал") == 5, "последние пять до потолка"
    exhausted = await flow.tap("req:search:1")
    flow.found = e2e.lots(2000, 30, brand="honda", model="lead")
    fresh = e2e.messages(await flow.tap("req:search:1"))[-1]
    assert "300" in str(fresh.text) and "17 ноября" in str(fresh.text)
    assert not any(data == "plan:subscribe" for _, data, _ in e2e.buttons(fresh)), exhausted


async def test_a_renewal_keeps_one_subscription_and_a_second_payload_adds_a_slot(
    flow: Flow,
) -> None:
    await flow.tap(f"bill:accept:{words.TERMS_VERSION}")
    await pay(flow, expiration=paid_until(flow))
    account = Account(1, fx.CLIENT)

    await pay(
        flow, charge="charge-2", update_id=4, first=False, expiration=paid_until(flow, 2 * MONTH)
    )
    assert len(flow.bill.payments) == 2
    assert await flow.paid.slots(account, flow.clock()) == 1, "продление — тот же слот"

    other = InvoicePayload(fx.CLIENT, words.TERMS_VERSION, "ba9876543210").encode()
    await flow.feed(
        fx.successful_payment(other, charge="charge-3", update_id=5, expiration=paid_until(flow))
    )
    assert await flow.paid.slots(account, flow.clock()) == 2, "каждая следующая подписка — ещё слот"
    assert flow.slots.syncs == [fx.CLIENT] * 3


async def test_a_lapsed_subscription_returns_the_account_to_the_free_ten(flow: Flow) -> None:
    from datetime import timedelta

    await flow.tap(f"bill:accept:{words.TERMS_VERSION}")
    await pay(flow, expiration=paid_until(flow))
    assert str((await flow.command("/plan"))[0].text).count("300") >= 1
    flow.clock.tick(timedelta(days=31))

    plan = str((await flow.command("/plan"))[0].text)

    assert plan.startswith("Бесплатно — 10 карточек за период"), plan


async def test_a_refund_by_the_owner_takes_the_subscription_away_and_is_idempotent(
    flow: Flow,
) -> None:
    await flow.tap(f"bill:accept:{words.TERMS_VERSION}")
    await pay(flow, expiration=paid_until(flow))
    account = Account(1, fx.CLIENT)

    answer = await flow.feed(fx.command("/refund charge-1 42", from_id=fx.OWNER, update_id=20))
    assert flow.session.sent(RefundStarPayment)[0].telegram_payment_charge_id == "charge-1"
    assert e2e.messages(answer)
    assert await flow.paid.slots(account, flow.clock()) == 0, "после возврата подписки нет"

    flow.session.calls.clear()
    await flow.feed(fx.refunded_payment(PAYLOAD, update_id=21))
    await flow.feed(fx.refunded_payment(PAYLOAD, update_id=22))
    assert await flow.paid.slots(account, flow.clock()) == 0


async def test_a_stranger_cannot_refund(flow: Flow) -> None:
    await flow.tap(f"bill:accept:{words.TERMS_VERSION}")
    await pay(flow, expiration=paid_until(flow))
    flow.session.calls.clear()

    await flow.feed(fx.command("/refund charge-1 42", from_id=fx.CLIENT, update_id=30))

    assert flow.session.calls == [] and flow.bill.refunded == set()


# ── 6. слежение: матчер → очередь → нотифаер → тема или General ─────────────


def outbox_from(world: mon.World, *, tg_user: int = 142) -> list[Row]:
    """То, что матчер положил в очередь, — строками `outbox` для нотифаера."""
    rows = [
        Row(
            id=index,
            user_id=101,
            recipient_id=tg_user,
            payload=dict(item["payload"]),
            subscription_id=1,
            scheduled_at=item["scheduled_at"],
        )
        for index, item in enumerate(world.delivery.queued, start=1)
    ]
    start = len(rows) + 1
    rows += [
        Row(
            id=start + index,
            user_id=101,
            recipient_id=tg_user,
            payload=dict(item["payload"]),
            subscription_id=1,
            scheduled_at=item["scheduled_at"],
        )
        for index, item in enumerate(world.delivery.notices)
    ]
    return rows


async def test_new_cards_reach_the_thread_of_their_search_and_only_ten_a_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = mon.install(
        monkeypatch,
        subscriptions=[mon.subscription(max_per_day=10)],
        page=[mon.listing(n) for n in range(1, 16)],
    )
    assert await MonitorAgent().tick(now=mon.NOW) == 10
    wire = Wire()
    delivery, _ = build(outbox_from(world), FakeTabs({1: 777}), wire)

    sent = await delivery.tick(now=mon.NOW)
    later = await delivery.tick(now=mon.NOW + OVERFLOW_DELAY)

    assert sent == 10 and {thread for _, _, thread in wire.sent[:10]} == {777}
    assert all("открыть оригинал" in text for _, text, _ in wire.sent[:10])
    assert later == 1
    assert "Сегодня подошло ещё 5 сверх 10 в сутки." in wire.sent[-1][1]
    assert wire.sent[-1][2] == 777, "сводка идёт в ту же тему"


async def test_without_a_thread_cards_go_to_general_and_a_dead_thread_falls_back_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = mon.install(
        monkeypatch,
        subscriptions=[mon.subscription(max_per_day=10)],
        page=[mon.listing(n) for n in range(1, 4)],
    )
    await MonitorAgent().tick(now=mon.NOW)
    plain = Wire()
    delivery, _ = build(outbox_from(world), None, plain)
    await delivery.tick(now=mon.NOW)
    assert [thread for _, _, thread in plain.sent] == [None, None, None], "без темы — General"

    dead_world = mon.install(
        monkeypatch,
        subscriptions=[mon.subscription(max_per_day=10)],
        page=[mon.listing(n) for n in range(1, 4)],
    )
    await MonitorAgent().tick(now=mon.NOW)
    gone = Wire(
        thread_error=TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message=GONE)
    )
    tabs = FakeTabs({1: 777})
    delivery, _ = build(outbox_from(dead_world), tabs, gone)
    await delivery.tick(now=mon.NOW)

    assert tabs.lost == [1], "связь с темой утрачена один раз"
    assert [thread for _, _, thread in gone.sent] == [None, None, None]
    assert sum(LOST_NOTE in text for _, text, _ in gone.sent) == 1, "приписка о теме — один раз"


async def test_what_was_sent_today_shrinks_the_room_and_the_overflow_is_counted_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = mon.install(
        monkeypatch,
        subscriptions=[mon.subscription(max_per_day=10)],
        page=[mon.listing(n) for n in range(1, 8)],
    )
    world.delivery.used = 10

    assert await MonitorAgent().tick(now=mon.NOW) == 0

    assert world.delivery.queued == [] and world.monitors.overflow[0][2] == 7
    assert world.delivery.advanced == [(1, 7)], (
        "курсор прошёл мимо — очередь вчерашнего не образуется"
    )


async def test_a_slot_without_a_paid_subscription_pauses_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Слот с оплаченным сроком: слот без срока (выдан владельцем) от оплаты не зависит.
    paid = mon.subscription(1, expires_at=mon.NOW + timedelta(days=30))
    world = mon.install(monkeypatch, subscriptions=[paid], page=[mon.listing(1)])

    class NoSlots:
        async def count(self, user_id: int, now: datetime) -> int | None:
            return 0

    agent = MonitorAgent(slots=NoSlots())
    assert await agent.tick(now=mon.NOW) == 0

    assert world.monitors.no_slot == {1: mon.NOW}, "пауза «нет слота» записана"
    assert world.delivery.queued == [] and agent.counters.paused_no_slot == 1


async def test_the_pass_cancels_lapsed_subscriptions_before_taking_new_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = mon.install(monkeypatch, subscriptions=[mon.subscription(1)], page=[mon.listing(1)])
    world.monitors.lapsed = 1

    await MonitorAgent().tick(now=mon.NOW)

    assert world.monitors.order[:3] == ["cancel", "mark_lapsed", "claim"], (
        "просрочка снимается до выдачи работы"
    )
    assert world.monitors.cancellations[0]["now"] == mon.NOW


async def test_an_edit_of_the_search_in_the_bot_changes_what_the_monitor_asks_for(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow.found = e2e.lots(1, 3)
    await flow.say("ищу скутер в Нячанге")
    await flow.say("до 300 долларов")
    current = (await flow.store.load(Client(fx.CLIENT, "dima"))).passport
    assert current is not None and current.passport.budget.max == 300
    world = mon.install(
        monkeypatch,
        subscriptions=[mon.subscription(1, passport=current)],
        page=[mon.listing(1)],
    )

    async def rate() -> float:
        return 26_000.0

    await MonitorAgent(rate=rate).tick(now=mon.NOW)

    asked = world.listings.asked[0][0]
    assert asked.city == "nha_trang"
    assert asked.max_price_vnd == Decimal("7800000"), "300 $ из правки стали потолком в донгах"


# ── 7. бюджет в EUR, голос, пустые и враждебные сообщения, чужие и старые кнопки ──


async def test_a_euro_budget_is_asked_again_in_dongs_or_dollars_and_search_goes_on(
    flow: Flow,
) -> None:
    flow.found = e2e.lots(1, 3)

    calls = await flow.say("ищу скутер в Нячанге до 300 евро")

    question = e2e.messages(calls)[-1]
    assert question.text == CURRENCY_ASK and "открыть оригинал" not in " ".join(e2e.texts(calls))
    data_of = [str(data) for _, data, _ in e2e.buttons(question)]
    assert any("USD" in d for d in data_of) and any("VND" in d for d in data_of)
    assert not any("EUR" in d or "RUB" in d for d in data_of), "кнопки только донги и доллары"
    usd = next(d for d in data_of if "USD" in d)
    shown = " ".join(e2e.texts(await flow.tap(usd)))
    assert shown.count("открыть оригинал") == 3


async def test_a_voice_message_shows_what_was_heard_and_then_searches(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def heard(**_: object) -> str:
        return "ищу скутер в Нячанге"

    monkeypatch.setattr(voice_input, "transcribe", heard)
    flow.found = e2e.lots(1, 3)

    calls = await flow.voice()

    said = e2e.texts(calls)
    assert said[0] == "Услышал: «ищу скутер в Нячанге»", "услышанное всегда показывается"
    assert "открыть оригинал" in " ".join(said)


async def test_an_unrecognised_or_too_long_voice_is_answered_in_words(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def nothing(**_: object) -> None:
        return None

    monkeypatch.setattr(voice_input, "transcribe", nothing)

    assert e2e.texts(await flow.voice()) == [voice_input.NOT_RECOGNISED]
    too_long = await flow.voice(duration=voice_input.MAX_VOICE_SECONDS + 1)
    assert e2e.texts(too_long) == [voice_input.TOO_LONG]
    assert flow.store.rows == []


HOSTILE = [
    "   ",
    "​",
    "🛵🛵🛵",
    "ж" * 5000,
    "<b>x</b> & <script>alert(1)</script> скутер",
    "ищу скутер\x00в Нячанге",
    "/start@RecVNbot deep-link",
    "/new",
    "/plan лишний аргумент",
    "/watch@RecVNbot",
    "/nonexistent",
    "/" + "a" * 300,
    "ищу " + "скутер " * 400,
]


@pytest.mark.parametrize("text", HOSTILE)
async def test_hostile_and_empty_messages_never_crash_the_dispatcher(flow: Flow, text: str) -> None:
    flow.found = e2e.lots(1, 3)

    calls = await flow.say(text)

    for sent in e2e.messages(calls):
        assert len(str(sent.text)) <= 4096, "ответ длиннее лимита Telegram"
        assert "<script>" not in str(sent.text)


async def test_a_bare_new_asks_what_to_look_for_and_the_next_message_is_the_search(
    flow: Flow,
) -> None:
    flow.found = e2e.lots(1, 3)

    asked = await flow.command("/new")
    answered = await flow.say("ищу скутер в Нячанге")

    assert e2e.texts(asked) == [threads.ASK_WHAT]
    assert "открыть оригинал" in " ".join(e2e.texts(answered))


async def test_an_unknown_command_is_never_a_search_phrase(flow: Flow) -> None:
    flow.found = e2e.lots(1, 3)

    calls = await flow.say("/terms2")

    assert e2e.texts(calls) == [wording_plan.UNKNOWN_COMMAND] and flow.store.rows == []


async def test_a_foreign_page_button_is_answered_as_expired_and_shows_nothing(flow: Flow) -> None:
    flow.found = wide_market()
    calls = await settle(flow, "ищу скутер в Нячанге")
    more = e2e.button(calls, "Ещё 5")

    stranger = await flow.tap(more, user=999)

    (alert,) = e2e.answered(stranger)
    assert alert.show_alert is True and alert.text == paging.EXPIRED
    assert e2e.messages(stranger) == []
    plan = await flow.command("/plan")
    assert str(plan[0].text).startswith("Бесплатно: использовано 5 из 10"), "квота хозяина цела"


async def test_a_stale_or_garbage_callback_is_answered_quietly(flow: Flow) -> None:
    flow.found = wide_market()
    calls = await settle(flow, "ищу скутер в Нячанге")
    more = e2e.button(calls, "Ещё 5")

    stale = await flow.tap_stale(more)
    garbage = await flow.tap("zzz:not-ours")
    unknown_page = await flow.tap("pg:nosuchtk:more:5")
    foreign_search = await flow.tap("req:search:1", user=999)
    missing_search = await flow.tap("req:search:777")

    assert e2e.messages(stale) == [] and e2e.messages(garbage) == []
    assert e2e.answered(unknown_page)[0].text == paging.EXPIRED
    assert [m.text for m in e2e.messages(foreign_search)] == [threads.NOT_FOUND]
    assert [m.text for m in e2e.messages(missing_search)] == [threads.NOT_FOUND]


async def test_an_old_question_button_after_the_answer_does_not_crash_or_overspend(
    flow: Flow,
) -> None:
    flow.found = wide_market()
    asked = await flow.say("ищу скутер в Нячанге")
    old = e2e.button(asked, "Lead")
    await flow.tap(old)

    again = await flow.tap(old)

    assert e2e.answered(again), "нажатие подтверждено"
    plan = await flow.command("/plan")
    used = re.search(r"использовано (\d+) из 10", str(plan[0].text))
    assert used is None or int(used.group(1)) <= 10


# ── 8. падение источника не роняет ответ ────────────────────────────────────


async def test_a_failing_source_gives_a_plain_answer_and_costs_no_quota(flow: Flow) -> None:
    flow.source_error = RuntimeError("chotot is down")

    calls = await flow.say("ищу скутер в Нячанге")

    assert wording.SEARCH_FAILED in e2e.texts(calls)
    plan = await flow.command("/plan")
    assert str(plan[0].text).startswith("Бесплатно — 10 карточек")
    flow.source_error = None
    flow.found = e2e.lots(1, 3)
    recovered = await flow.tap("req:search:1")
    assert "открыть оригинал" in " ".join(e2e.texts(recovered)), "после сбоя поиск живой"


async def test_an_empty_catalogue_says_nothing_was_found(flow: Flow) -> None:
    flow.found = []

    calls = await flow.say("ищу скутер в Нячанге")

    assert any(text.startswith(wording.NOTHING_FOUND[:20]) for text in e2e.texts(calls))
    assert flow.ledger.rows(1) == [], "ничего не выдано — период не начат"


async def test_a_quota_ledger_failure_is_an_honest_message_not_cards_past_the_limit(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_: object, **__: object) -> None:
        raise ConnectionError("db is down")

    monkeypatch.setattr(flow.ledger, "reserve", broken)
    flow.found = e2e.lots(1, 3)

    calls = await flow.say("ищу скутер в Нячанге")

    joined = " ".join(e2e.texts(calls))
    assert wording_plan.QUOTA_UNAVAILABLE in joined and "открыть оригинал" not in joined


async def test_the_refinement_the_header_suggests_is_not_refused_as_a_second_search(
    flow: Flow,
) -> None:
    flow.found = wide_market()
    await settle(flow, "ищу скутер в Нячанге")

    calls = await flow.say("honda lead")

    assert REFUSED_FREE not in e2e.texts(calls)


async def test_the_watch_panel_shows_the_slots_the_searches_and_the_limit(flow: Flow) -> None:
    flow.found = e2e.lots(1, 3)
    await flow.say("ищу скутер в Нячанге")

    (panel,) = e2e.messages(await flow.command("/watch"))

    text = str(panel.text)
    assert "Мои слежения" in text and "Слоты: куплено 0, занято 0" in text
    assert "Скутер, Нячанг" in text or "скутер" in text.lower()


async def test_the_greeting_states_the_money_rule_with_the_numbers_of_the_plan(flow: Flow) -> None:
    (sent,) = e2e.messages(await flow.command("/start"))

    text = str(sent.text)
    assert f"{plans.FREE_CARDS_PER_PERIOD} карточек" in text and "первой выданной карточки" in text
    assert "сузить поиск" in text and f"{plans.SUBSCRIPTION_STARS} ⭐" in text
    assert f"до {plans.PAID_CARDS_PER_PERIOD} карточек" in text
    assert "повторно не считается" in text


async def test_the_greeting_does_not_promise_many_searches_to_a_one_search_account(
    flow: Flow,
) -> None:
    (sent,) = e2e.messages(await flow.command("/start"))

    text = str(sent.text)
    promises_many = "/new" in text and "несколько" in text
    names_the_cap = re.search(r"один поиск|1 поиск|одного поиска|до 10 поисков", text) is not None
    assert not promises_many or names_the_cap


async def test_the_owner_has_no_quota_line_and_no_limit(flow: Flow) -> None:
    flow.found = wide_market()

    calls = await flow.say("ищу скутер в Нячанге", user=e2e.OWNER)
    for _ in range(4):
        if "открыть оригинал" in " ".join(e2e.texts(calls)):
            break
        calls = await flow.tap(e2e.button(calls, "Любой"), user=e2e.OWNER)

    page = " ".join(e2e.texts(calls))
    assert "открыть оригинал" in page and "осталось" not in page.lower()
    plan = await flow.command("/plan", user=e2e.OWNER)
    assert plan, "у владельца /plan отвечает"


async def test_replace_after_new_removes_the_only_search_and_asks_what_to_look_for(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow.found = e2e.lots(1, 3)
    await flow.say("ищу скутер в Нячанге")
    archived: list[int] = []

    async def archive(_client: Any, root: int) -> bool:
        archived.append(root)
        return True

    monkeypatch.setattr(watch_flow, "archive", archive)
    refused = await flow.command("/new")
    data = e2e.button(refused, "Заменить")

    calls = await flow.tap(data)

    assert len(archived) == 1
    assert "Прежний поиск убран" in " ".join(e2e.texts(calls))


async def test_keep_after_new_leaves_the_search_alone(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow.found = e2e.lots(1, 3)
    await flow.say("ищу скутер в Нячанге")

    async def archive(*_a: Any, **_k: Any) -> bool:
        raise AssertionError("«Оставить» не должно ничего убирать")

    monkeypatch.setattr(watch_flow, "archive", archive)
    refused = await flow.command("/new")

    calls = await flow.tap(e2e.button(refused, "Оставить"))

    assert "Оставил текущий поиск" in " ".join(e2e.texts(calls))
    assert len({r.root for r in flow.store.rows}) == 1


@pytest.mark.parametrize("note", ["honda lead", "автомат", "в Нячанге", "до 300 долларов"])
async def test_an_addendum_under_the_results_refines_the_same_branch(flow: Flow, note: str) -> None:
    flow.found = wide_market()
    await settle(flow, "ищу скутер в Нячанге")

    calls = await flow.say(note)

    assert REFUSED_FREE not in e2e.texts(calls)
    assert len({r.root for r in flow.store.rows}) == 1


@pytest.mark.parametrize("subject", ["ищу квартиру в Нячанге", "ищу скутер в Дананге"])
async def test_a_new_subject_or_city_is_still_a_new_search_and_hits_the_free_limit(
    flow: Flow, subject: str
) -> None:
    flow.found = wide_market()
    await settle(flow, "ищу скутер в Нячанге")

    calls = await flow.say(subject)

    assert REFUSED_FREE in e2e.texts(calls)
