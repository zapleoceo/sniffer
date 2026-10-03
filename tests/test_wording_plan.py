"""Слова про лимит и подписку: даты, остаток, исчерпание, `/plan`, приветствие.

Чистые функции, без базы и Telegram. Здесь закреплено то, что обязано остаться
верным после правки любой формулировки: числа берутся из `domain/plans`, дата
считается по вьетнамскому календарю, подписчику на потолке подписку не продают,
а человек читает «поиск», а не «запрос».
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import UTC, datetime

import pytest

from sniffer.bot import wording, wording_plan
from sniffer.bot.naming import plural
from sniffer.domain.plans import FREE_CARDS_PER_PERIOD, PAID_CARDS_PER_PERIOD, SUBSCRIPTION_STARS
from sniffer.domain.quota import Standing

END = datetime(2026, 11, 17, 2, 30, tzinfo=UTC)  # 17 ноября, 09:30 по Хошимину
MONTHS = [
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]


# ── дата: календарь Вьетнама, родительный падеж ─────────────────────────────


def test_the_date_is_written_in_the_vietnamese_calendar_not_in_utc() -> None:
    """16 ноября 18:00 UTC — это уже 17-е по Хошимину: человек живёт там."""
    assert wording_plan.ru_date(datetime(2026, 11, 16, 18, 0, tzinfo=UTC)) == "17 ноября"
    assert wording_plan.ru_date(datetime(2026, 11, 16, 16, 59, tzinfo=UTC)) == "16 ноября"


@pytest.mark.parametrize(("month", "name"), list(enumerate(MONTHS, 1)))
def test_every_month_is_in_the_genitive(month: int, name: str) -> None:
    assert wording_plan.ru_date(datetime(2026, month, 10, 5, 0, tzinfo=UTC)) == f"10 {name}"


# ── остаток над выдачей ─────────────────────────────────────────────────────


def test_a_free_person_sees_how_many_free_cards_are_left_until_a_concrete_date() -> None:
    assert (
        wording_plan.balance_line(FREE_CARDS_PER_PERIOD, 7, END)
        == "Бесплатно осталось 7 из 10 до 17 ноября."
    )


def test_a_subscriber_sees_the_balance_without_the_word_free() -> None:
    line = wording_plan.balance_line(PAID_CARDS_PER_PERIOD, 241, END)

    assert line == "Осталось 241 из 300 до 17 ноября."
    assert "Бесплатно" not in str(line)


@pytest.mark.parametrize(
    ("limit", "remaining", "end"),
    [(None, None, None), (None, 5, END), (10, None, END), (10, 5, None)],
)
def test_without_a_limit_a_remainder_or_a_date_there_is_no_balance_line(
    limit: int | None, remaining: int | None, end: datetime | None
) -> None:
    """Владелец и слежение: строки остатка у них нет, а выдуманной цифры не бывает."""
    assert wording_plan.balance_line(limit, remaining, end) is None


# ── честная строка про то, что лимит не пустил ──────────────────────────────


@pytest.mark.parametrize(
    ("count", "noun"),
    [
        (1, "подходящий вариант"),
        (2, "подходящих варианта"),
        (5, "подходящих вариантов"),
        (11, "подходящих вариантов"),
        (21, "подходящий вариант"),
        (33, "подходящих варианта"),
    ],
)
def test_the_number_of_held_back_cards_is_declined_like_a_person_would_say_it(
    count: int, noun: str
) -> None:
    line = wording_plan.more_line(count, limit=FREE_CARDS_PER_PERIOD, renews=END, selling=True)

    assert line == f"Ещё {count} {noun} — по подписке {SUBSCRIPTION_STARS} ⭐/мес."


def test_a_subscriber_at_the_ceiling_is_not_sold_a_subscription_that_adds_nothing() -> None:
    """Потолок один на аккаунт; вторая подписка прибавляет слежение, а не карточки."""
    line = wording_plan.more_line(40, limit=PAID_CARDS_PER_PERIOD, renews=END, selling=True)

    assert "подписке" not in line
    assert "после обновления лимита 17 ноября" in line


def test_the_plural_rule_is_the_one_the_cards_use() -> None:
    assert plural(21, ("день", "дня", "дней")) == "день"
    assert plural(112, ("день", "дня", "дней")) == "дней"


# ── исчерпание ──────────────────────────────────────────────────────────────


def test_the_offer_names_what_ended_when_it_renews_and_both_roads_with_the_price() -> None:
    text = wording_plan.exhausted_offer(total=8100, renews=END)

    assert "закончились — новые откроются 17 ноября." in text
    assert f"Бесплатные {FREE_CARDS_PER_PERIOD} карточек" in text
    assert "Сейчас подходящих объявлений: 8100" in text
    assert f"{SUBSCRIPTION_STARS} ⭐ в месяц" in text and f"до {PAID_CARDS_PER_PERIOD}" in text
    assert "Или подождать до 17 ноября" in text and "/requests" in text


def test_the_offer_after_a_partial_issue_does_not_repeat_the_total() -> None:
    assert "подходящих объявлений" not in wording_plan.exhausted_offer(total=None, renews=END)


def test_the_short_answer_has_no_payment_pitch() -> None:
    """Предложение сегодня уже было: второй раз тот же текст с оплатой — давление."""
    text = wording_plan.exhausted_short(total=37, renews=END)

    assert "17 ноября" in text and "37" in text
    assert "подписк" not in text.lower() and "⭐" not in text


def test_the_ceiling_message_is_information_not_a_sale() -> None:
    text = wording_plan.exhausted_cap(total=500, renews=END)

    assert str(PAID_CARDS_PER_PERIOD) in text and "17 ноября" in text
    assert "подписк" not in text.lower() and "⭐" not in text


def test_without_a_known_renewal_date_the_text_still_says_when_in_words() -> None:
    assert "в начале следующего периода" in wording_plan.exhausted_short(total=1, renews=None)


# ── /plan ───────────────────────────────────────────────────────────────────


def test_the_plan_of_a_free_person_in_the_middle_of_a_period() -> None:
    text = wording_plan.plan_text(Standing(used=5, limit=10, period_end=END), selling=True)

    assert text.startswith("Бесплатно: использовано 5 из 10 карточек.\nОбновится 17 ноября.")
    assert f"{SUBSCRIPTION_STARS} ⭐" in text


def test_the_plan_before_the_first_card_says_the_period_has_not_started() -> None:
    text = wording_plan.plan_text(Standing(used=0, limit=10, period_end=None), selling=True)

    assert "начнётся с первой выданной карточки" in text and "использовано 0" in text


def test_the_plan_of_a_subscriber_is_not_an_advertisement() -> None:
    text = wording_plan.plan_text(Standing(used=59, limit=300, period_end=END), selling=True)

    assert text == "По подписке: использовано 59 из 300 карточек.\nОбновится 17 ноября."


def test_the_plan_after_a_lapsed_subscription_tells_the_truth_about_both_numbers() -> None:
    """Выдано было 250, потолок снова 10: «250 из 10» читалось бы как ошибка."""
    text = wording_plan.plan_text(Standing(used=250, limit=10, period_end=END), selling=True)

    assert "использовано 250, потолок сейчас 10" in text


def test_the_owner_is_told_there_is_no_limit() -> None:
    standing = Standing(used=9999, limit=None, period_end=None)

    assert wording_plan.plan_text(standing, selling=True) == "Для вас лимита карточек нет."


# ── приветствие и словарь ───────────────────────────────────────────────────


def test_the_greeting_states_the_rule_with_the_numbers_of_the_plans_module() -> None:
    greeting = wording.GREETING

    assert f"{FREE_CARDS_PER_PERIOD} карточек на ваш Telegram-аккаунт за период" in greeting
    assert f"подписка {SUBSCRIPTION_STARS} ⭐ в месяц" in greeting
    assert f"до {PAID_CARDS_PER_PERIOD} карточек" in greeting
    assert "лучше сразу сузить поиск" in greeting
    assert "/new" in greeting and "/requests" in greeting and "/plan" in greeting


def test_every_text_about_the_limit_says_search_not_request_or_branch() -> None:
    """Одно слово на одну вещь: «поиск». «Запрос» и «ветка» — внутренние имена."""
    texts = [
        wording.GREETING,
        wording_plan.QUOTA_UNAVAILABLE,
        wording_plan.UNKNOWN_COMMAND,
        wording_plan.SUBSCRIBE_LABEL,
        wording_plan.more_line(3, limit=FREE_CARDS_PER_PERIOD, renews=END, selling=True),
        wording_plan.exhausted_offer(total=5, renews=END),
        wording_plan.exhausted_short(total=5, renews=END),
        wording_plan.exhausted_cap(total=5, renews=END),
        wording_plan.exhausted_closed(total=5, renews=END),
        wording_plan.plan_text(Standing(1, 10, END), selling=True),
    ]

    for text in texts:
        lowered = text.lower().replace("/requests", "")
        assert "запрос" not in lowered and "ветк" not in lowered, text


def test_the_texts_are_valid_telegram_html() -> None:
    """Голый «<», «>» или «&» Telegram отвергает целиком: сообщение не дойдёт."""
    plain = [
        wording_plan.QUOTA_UNAVAILABLE,
        wording_plan.UNKNOWN_COMMAND,
        wording_plan.exhausted_offer(total=5, renews=END),
        wording_plan.exhausted_short(total=5, renews=END),
        wording_plan.exhausted_cap(total=5, renews=END),
        wording_plan.exhausted_closed(total=5, renews=END),
        wording_plan.plan_text(Standing(1, 10, END), selling=True),
        wording_plan.more_line(2, limit=10, renews=END, selling=True),
    ]
    for text in plain:
        assert not re.search(r"[<>&]", text), text
    assert not re.search(r"[<>&]", re.sub(r"</?i>", "", wording.GREETING))


PROBE = """
import sys
import sniffer.bot.wording_plan
heavy = [
    m for m in sys.modules
    if m.startswith(("sniffer.db", "sqlalchemy", "aiogram", "telethon"))
]
print(len(heavy))
"""


def test_importing_the_plan_wording_does_not_pull_the_database_or_telegram() -> None:
    """Как у `wording.py`: формулировку проверяют без Postgres, и это не должно тихо исчезнуть."""
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", PROBE], capture_output=True, text=True, timeout=120
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "0", f"поднято модулей базы или Telegram: {done.stdout.strip()}"


def test_the_closed_text_has_facts_and_no_call_to_buy() -> None:
    text = wording_plan.exhausted_closed(total=8100, renews=END)

    assert "Использовано 10 из 10" in text and "17 ноября" in text and "8100" in text
    assert "⭐" not in text and "подписк" not in text.lower()
    assert "в начале следующего периода" in wording_plan.exhausted_closed(total=None, renews=None)


def test_a_free_person_is_not_pointed_to_a_subscription_while_sales_are_off() -> None:
    line = wording_plan.more_line(5, limit=FREE_CARDS_PER_PERIOD, renews=END, selling=False)
    assert "по подписке" not in line and "17 ноября" in line
    assert "подписк" not in wording_plan.plan_text(Standing(0, 10, None), selling=False).lower()


@pytest.mark.parametrize(
    ("flag", "owner", "expected"),
    [(False, 0, False), (False, 5, False), (True, 0, False), (True, 5, True)],
)
def test_sales_are_open_only_with_the_flag_and_an_owner(
    flag: bool, owner: int, expected: bool
) -> None:
    from sniffer.config import Settings

    cfg = Settings(_env_file=None, sales_enabled=flag, owner_chat_id=owner)  # type: ignore[call-arg]
    assert cfg.selling is expected
