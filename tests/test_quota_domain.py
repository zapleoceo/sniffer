"""Чистая часть квоты: сколько допустить, какой период, сколько стоит тариф.

Без базы: решение «что из запрошенного пустить» — функция от четырёх чисел, а
границы периода — функция от якоря. Всё, что зависит от Postgres (блокировка,
уникальность, CHECK), проверяется в `test_quota_db.py` на живой базе, а здесь —
правила, которые не должны зависеть от того, поднята ли она.
"""

from __future__ import annotations

import random
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sniffer.domain.plans import (
    FREE_CARDS_PER_PERIOD,
    PAID_CARDS_PER_PERIOD,
    SUBSCRIPTION_STARS,
    card_cap,
)
from sniffer.domain.quota import (
    Admission,
    Channel,
    Decision,
    decide,
    numbered_period,
    unique,
)
from sniffer.domain.quota_period import VIETNAM, add_months


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# ── сколько допустить ───────────────────────────────────────────────────────


def test_a_partial_issue_gives_the_best_cards_up_to_the_remainder() -> None:
    """Осталось 4, подходит 37: бесплатно уходят четыре лучшие, остальные ждут."""
    requested = list(range(1, 38))

    decision = decide(requested, seen=(), used=6, limit=10)

    assert decision.granted == (1, 2, 3, 4)
    assert decision.withheld == tuple(range(5, 38))
    assert decision.remaining == 0
    assert decision.repeated == ()


def test_what_the_person_has_already_seen_in_the_period_stays_free_at_a_zero_remainder() -> None:
    """Лимит исчерпан, а карточки, за которые уже заплачено, доступны."""
    decision = decide([1, 5, 2, 6], seen={5, 6}, used=10, limit=10)

    assert decision == Decision(granted=(), repeated=(5, 6), withheld=(1, 2), remaining=0)


def test_a_repeated_card_never_takes_a_place_from_a_new_one() -> None:
    """«Виденное» не считается дважды: оно не расходует остаток."""
    decision = decide([7, 8, 9, 1, 2], seen={7, 8, 9}, used=9, limit=10)

    assert decision.granted == (1,)
    assert decision.repeated == (7, 8, 9)
    assert decision.withheld == (2,)
    assert decision.remaining == 0


def test_the_order_of_the_issue_is_kept_in_every_part() -> None:
    decision = decide([9, 3, 7, 1, 5], seen={7}, used=7, limit=10)

    assert decision.granted == (9, 3, 1)
    assert decision.repeated == (7,)
    assert decision.withheld == (5,)


def test_the_same_card_twice_in_one_issue_is_one_card() -> None:
    decision = decide([4, 4, 5, 4], seen=(), used=0, limit=10)

    assert decision.granted == (4, 5)
    assert decision.remaining == 8
    assert unique([4, 4, 5, 4]) == (4, 5)


def test_without_a_limit_everything_new_is_granted_and_nothing_is_counted() -> None:
    """Владелец и слежение: потолка нет, журнал всё равно пишется."""
    decision = decide([1, 2, 3], seen={2}, used=9999, limit=None)

    assert decision == Decision(granted=(1, 3), repeated=(2,), withheld=(), remaining=None)


def test_a_ceiling_lowered_below_the_spent_amount_leaves_zero_not_a_negative() -> None:
    """Подписка кончилась посреди периода, а выдано было 250 при потолке 10."""
    decision = decide([1, 2], seen=(), used=250, limit=10)

    assert decision.granted == ()
    assert decision.withheld == (1, 2)
    assert decision.remaining == 0


def test_nothing_requested_means_nothing_decided() -> None:
    assert decide([], seen={1}, used=3, limit=10) == Decision((), (), (), 7)


@pytest.mark.parametrize(("used", "limit"), [(-1, 10), (0, -1)])
def test_a_negative_count_is_a_bug_of_the_caller_and_fails_loudly(used: int, limit: int) -> None:
    with pytest.raises(ValueError, match="отрицательн"):
        decide([1], seen=(), used=used, limit=limit)


def test_the_decision_adds_up_for_every_small_combination() -> None:
    """Свойство, а не пример: допущено + виденное + отложенное = запрошено, остаток сходится."""
    generator = random.Random(20261003)  # noqa: S311 -- воспроизводимая выборка, не секрет
    for _ in range(2000):
        ids = [generator.randrange(30) for _ in range(generator.randrange(0, 25))]
        seen = {generator.randrange(30) for _ in range(generator.randrange(0, 12))}
        limit = generator.choice([None, 0, 3, 10])
        used = generator.randrange(0, 14)

        decision = decide(ids, seen=seen, used=used, limit=limit)

        assert set(decision.granted) | set(decision.repeated) | set(decision.withheld) == set(ids)
        assert not set(decision.granted) & set(decision.repeated)
        assert not set(decision.granted) & set(decision.withheld)
        assert set(decision.repeated) <= seen
        assert not set(decision.granted) & seen
        if limit is not None:
            assert len(decision.granted) <= max(0, limit - used)
            assert decision.remaining == max(0, limit - used) - len(decision.granted)
            # Новые берутся строго по порядку: отложены только те, что стоят позже допущенных.
            fresh = [i for i in dict.fromkeys(ids) if i not in seen]
            assert fresh == [*decision.granted, *decision.withheld]


# ── период ──────────────────────────────────────────────────────────────────


def test_a_period_number_is_the_k_whose_boundary_is_the_start() -> None:
    anchor = at("2027-01-31T10:00:00")

    assert numbered_period(anchor, anchor)[0] == 0
    assert numbered_period(anchor, at("2027-03-01T00:00:00"))[0] == 1
    assert numbered_period(anchor, at("2027-04-05T00:00:00"))[0] == 2
    # От якоря, а не от прошлой границы: третий период начинается 30 апреля, а не 28-го.
    assert numbered_period(anchor, at("2027-05-01T00:00:00"))[1].start.strftime("%m-%d") == "04-30"


def test_a_moment_before_the_anchor_is_the_first_period() -> None:
    """Часы двух процессов расходятся на миллисекунды: номер не уходит в минус."""
    anchor = at("2026-10-17T09:30:00")

    number, period = numbered_period(anchor, anchor - timedelta(milliseconds=5))

    assert number == 0
    assert period.start == anchor


def test_the_boundary_belongs_to_the_next_period() -> None:
    anchor = at("2026-10-17T09:30:00")
    _, first = numbered_period(anchor, anchor)

    assert numbered_period(anchor, first.end - timedelta(microseconds=1))[0] == 0
    assert numbered_period(anchor, first.end)[0] == 1


def test_a_naive_moment_fails_loudly() -> None:
    with pytest.raises(ValueError, match="часовым поясом"):
        numbered_period(datetime(2026, 10, 17, 9, 30), at("2026-10-18T00:00:00"))


def test_a_number_that_does_not_match_its_start_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сверка «граница k равна началу периода» — страховка от второй, разошедшейся формулы."""
    from sniffer.domain import quota

    monkeypatch.setattr(quota, "add_months", lambda moment, months, *rest: moment)

    with pytest.raises(ArithmeticError, match="не сходится"):
        numbered_period(at("2027-01-31T10:00:00"), at("2027-03-01T00:00:00"))


def test_the_number_agrees_with_a_step_by_step_count_on_random_pairs() -> None:
    """Номер — наибольшее k, при котором граница не позже «сейчас»: считаем лесенкой."""
    generator = random.Random(20261004)  # noqa: S311 -- воспроизводимая выборка, не секрет
    base = at("2024-01-01T00:00:00")
    for _ in range(2000):
        anchor = base + timedelta(seconds=generator.randrange(3 * 365 * 86_400))
        now = anchor + timedelta(seconds=generator.randrange(2 * 365 * 86_400))

        number, period = numbered_period(anchor, now)

        stepwise = 0
        while add_months(anchor, stepwise + 1) <= now:
            stepwise += 1
        assert number == stepwise, (anchor, now)
        assert period.start == add_months(anchor, number)
        assert period.end == add_months(anchor, number + 1)


# ── тариф и каналы ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(("slots", "cap"), [(0, 10), (1, 300), (2, 300), (7, 300)])
def test_one_live_slot_makes_the_ceiling_three_hundred_and_more_slots_do_not_raise_it(
    slots: int, cap: int
) -> None:
    assert card_cap(slots) == cap


def test_the_plan_numbers_are_the_owner_decisions_of_03_10_2026() -> None:
    assert (FREE_CARDS_PER_PERIOD, PAID_CARDS_PER_PERIOD, SUBSCRIPTION_STARS) == (10, 300, 10)


def test_a_negative_slot_count_is_a_bug_of_the_caller() -> None:
    with pytest.raises(ValueError, match="отрицательн"):
        card_cap(-1)


def test_only_the_monitor_does_not_spend_the_ceiling() -> None:
    assert [channel for channel in Channel if not channel.spends_cap] == [Channel.MONITOR]


def test_an_admission_names_what_may_be_shown() -> None:
    admission = Admission(granted=(1, 2), repeated=(9,), withheld=(5,))

    assert admission.shown == (1, 2, 9)
    assert admission.allows(9) and admission.allows(1)
    assert not admission.allows(5)
    assert not Admission().allows(1)


# ── схема и домен не расходятся ─────────────────────────────────────────────

DDL = Path(__file__).parents[1] / "infra" / "sql" / "010_quota_ledger.sql"


def ddl_text() -> str:
    return re.sub(r"--[^\n]*", "", DDL.read_text(encoding="utf-8"))


def test_the_channel_list_in_the_ddl_is_exactly_the_enum() -> None:
    """Две копии одного списка (CHECK и `Channel`) обязаны совпадать: проверяет тест, не память."""
    found = re.search(r"channel\s+TEXT\s+NOT NULL CHECK \(channel IN \(([^)]*)\)\)", ddl_text())

    assert found, "в DDL не нашёлся CHECK по каналам"
    listed = {value.strip().strip("'") for value in found.group(1).split(",")}
    assert listed == {channel.value for channel in Channel}


def test_the_ddl_formula_uses_the_calendar_of_the_domain() -> None:
    """Домен считает по Вьетнаму (UTC+7), и CHECK обязан считать по тому же календарю."""
    zones = set(re.findall(r"AT TIME ZONE '([^']+)'", ddl_text()))

    assert zones == {"Asia/Ho_Chi_Minh"}
    assert VIETNAM.tzname(None) == "Asia/Ho_Chi_Minh"
    assert VIETNAM.utcoffset(None) == timedelta(hours=7)


def test_the_boundary_check_stays_on_one_line() -> None:
    """Парсер `test_db_models.py` читает первое слово КАЖДОЙ строки как имя колонки."""
    lines = [line for line in ddl_text().splitlines() if "period_start =" in line]

    assert len(lines) == 1
    assert lines[0].strip().startswith("CHECK (") and lines[0].rstrip().endswith("),")
    assert "period_end =" in lines[0], "обе границы проверяются одним условием"


def squashed_ddl() -> str:
    return " ".join(ddl_text().split())


@pytest.mark.parametrize(
    "barrier",
    [
        "UNIQUE (period_id, listing_id)",
        "UNIQUE (user_id, period_no)",
        "UNIQUE (id, user_id)",
        "FOREIGN KEY (user_id, anchor_at) REFERENCES users (id, quota_anchor_at)",
        "FOREIGN KEY (period_id, user_id) REFERENCES quota_periods (id, user_id) ON DELETE CASCADE",
        "CREATE UNIQUE INDEX IF NOT EXISTS users_id_anchor_uidx ON users (id, quota_anchor_at)",
        "listing_id BIGINT NOT NULL REFERENCES listings(id),",
        "CREATE INDEX IF NOT EXISTS offer_views_unconfirmed_idx ON offer_views (shown_at) "
        "WHERE delivered_at IS NULL",
    ],
)
def test_the_ddl_keeps_the_barriers_the_algorithm_relies_on(barrier: str) -> None:
    """Уникальность, составные ключи и «без CASCADE на карточку» — на них держится «ровно»."""
    assert barrier in squashed_ddl()


def test_the_ddl_has_no_counter_trigger_and_no_used_column() -> None:
    """Правку данных цепочке запрещает `test_sql_chain.py`; здесь — решение этого пакета.

    Число занятых карточек — `count(*)` под блокировкой строки периода. Триггер со
    счётчиком `used` потребовал бы UPDATE в цепочке, а колонка, которую ведёт триггер,
    разошлась бы с журналом при первой же ручной правке.
    """
    ddl = squashed_ddl().upper()

    assert "TRIGGER" not in ddl
    assert " USED " not in ddl and "USED INT" not in ddl
