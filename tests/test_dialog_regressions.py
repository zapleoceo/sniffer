"""Разбор живого диалога 04.10.2026: счёт, кросспосты, цена без значения (F2, п. 4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sniffer.bot import wording
from sniffer.bot.presenter import Reply, present
from sniffer.domain.passport import Budget, Category, Currency, Intent, Passport
from sniffer.search.relevance import rank_items
from sniffer.sources.base import RawItem

NOW = datetime(2026, 10, 4, 6, 0, tzinfo=UTC)
RATE = 25_000.0


def lot(
    n: int, price: int | None, text: str | None = None, *, hours: int = 5, kind: str = "motorbike"
) -> RawItem:
    return RawItem(
        source="archive",
        external_id=str(n),
        url=f"https://t.me/c/1/{n}",
        title=f"Honda Vision {n}" if kind == "motorbike" else f"Квартира {n}",
        text=text if text is not None else f"Honda Vision, состояние хорошее, лот номер {n}",
        price_raw="" if price is None else str(price),
        price_vnd=price,
        posted_at=NOW - timedelta(hours=hours),
        raw={"listing_id": n, "category": kind, "attributes": {}},
    )


def passport(max_usd: int | None) -> Passport:
    budget = Budget(max=max_usd, currency=Currency.USD) if max_usd else Budget()
    return Passport(intent=Intent.BUY, category=Category.MOTORBIKE, city="nha_trang", budget=budget)


FLAT = Passport(intent=Intent.RENT, category=Category.APARTMENT, city="nha_trang")


def market() -> list[RawItem]:
    return [lot(n, 3_000_000 + n * 400_000) for n in range(40)] + [
        lot(100 + n, None) for n in range(5)
    ]


def count(max_usd: int) -> int:
    return len(rank_items(passport(max_usd), market(), usd_vnd=RATE, now=NOW))


def test_tightening_the_budget_never_grows_the_count() -> None:
    counts = [count(limit) for limit in (1000, 600, 400, 300, 210, 147, 100, 60)]
    assert counts == sorted(counts, reverse=True), counts


def test_a_capped_window_is_always_called_a_lower_bound() -> None:
    """Причина «95 → 97 при сужении»: выборка источника упирается в потолок, и счёт — не точный.

    Каждый шаг сужения берёт те же сто строк уже из более дешёвых, поэтому число после
    отсева способно вырасти. Честно это только словами «не меньше».
    """
    for limit in (300, 210, 147):
        header = wording.result_header(passport(limit), 97, 5, capped=True)
        assert "не меньше 97" in header


def test_one_lot_posted_to_two_chats_with_different_footers_is_shown_once() -> None:
    body = (
        "AN-HOME аренда начинается здесь Роскошная 2-комнатная квартира в Нячанге площадь 62 метра "
        "вид на море полностью меблирована готова к заселению залог один месяц консультация подбор"
    )
    first = lot(1, 18_000_000, body + " чат vietavito", kind="apartment")
    second = lot(2, 18_000_000, body + " чат arenda", kind="apartment")
    other = lot(
        3,
        18_000_000,
        "Совсем другая квартира: студия 26 метров на Чан Фу без мебели с кухней",
        kind="apartment",
    )
    shown = rank_items(
        FLAT,
        [first, second, other],
        usd_vnd=RATE,
        now=NOW,
    )
    assert [item.external_id for item in shown].count("2") + [
        item.external_id for item in shown
    ].count("1") == 1
    assert "3" in {item.external_id for item in shown}


def test_two_lots_of_one_template_with_different_prices_stay_two() -> None:
    body = (
        "AN-HOME аренда Роскошная 2-комнатная квартира в Нячанге площадь 62 метра вид на море "
        "полностью меблирована готова к заселению залог один месяц консультация подбор"
    )
    shown = rank_items(
        FLAT,
        [
            lot(1, 18_000_000, body, kind="apartment"),
            lot(2, 25_000_000, body + " этаж выше", kind="apartment"),
        ],
        usd_vnd=RATE,
        now=NOW,
    )
    assert len(shown) == 2


def test_with_a_budget_known_prices_come_before_a_lot_without_a_price() -> None:
    fresh_unpriced = lot(500, None, hours=1)
    older_priced = lot(501, 5_000_000, "Honda Vision старше", hours=80)
    shown = rank_items(passport(1000), [fresh_unpriced, older_priced], usd_vnd=RATE, now=NOW)
    assert [item.external_id for item in shown] == ["501", "500"]


def test_without_a_budget_freshness_still_decides() -> None:
    fresh_unpriced = lot(500, None, hours=1)
    older_priced = lot(501, 5_000_000, "Honda Vision старше", hours=80)
    shown = rank_items(passport(None), [fresh_unpriced, older_priced], usd_vnd=RATE, now=NOW)
    assert shown[0].external_id == "500"


def test_the_header_says_how_many_have_no_price_when_a_budget_was_asked() -> None:
    header = wording.result_header(passport(300), 40, 5, unpriced=7)
    assert "без цены — 7" in header
    assert "без цены" not in wording.result_header(passport(300), 40, 5)
    assert "без цены" not in wording.result_header(passport(None), 40, 5, unpriced=7)


def test_the_presenter_counts_unpriced_lots_of_the_whole_selection() -> None:
    class Found:
        items = [lot(n, 5_000_000) for n in range(8)] + [lot(100 + n, None) for n in range(3)]
        status = None
        deferred = False
        capped = False

    reply: Reply = present(passport(300), Found(), root=1)
    assert "без цены — 3" in reply.text
