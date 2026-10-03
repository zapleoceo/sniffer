"""Проход догона без базы: курсор, пачки, сухой прогон, идемпотентность, пропуски.

База заменена памятью с теми же гарантиями, что обещает репозиторий: страница
по возрастанию id, запись патча «как база», отказ одной строки. SQL и гонка с
воркером проверены отдельно (`test_enrich_guard.py` без базы,
`test_db_enrichment.py` на живой). Разбор текста подменён заглушкой (`FACTS`):
здесь проверяется обход и учёт, а не числа настоящего разбора.
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from sniffer.domain.listing_patch import (
    ListingPatch,
    ListingWithText,
    WriteResult,
    WriteStatus,
)
from sniffer.domain.prices import PriceFact
from sniffer.domain.records import Listing
from sniffer.pipeline.enrich import DerivationFailed, derive
from sniffer.pipeline.enrich_price import PriceDerivation
from sniffer.worker.enrich import MAX_ROW_WARNINGS, ByVerdict, Derive, EnrichPass, write_each
from sniffer.worker.enrich_report import END, LIMIT, EnrichReport
from tests.enrich_support import fact, row

Item = tuple[ListingWithText, ListingPatch]

# Что «нашёл бы разбор» в тексте-ключе строки: политика, а не числа настоящего разбора.
FACTS: dict[str, PriceFact | None] = {
    "fill": fact(12_500_000, period="month"),
    "replace": fact(5_500_000, period="month"),
    "disagree": fact(9_000_000, period="month"),
    "lost": None,
    "rate": fact(250_000, period="day"),
    "fill-sell": fact(21_000_000),
    "same": fact(12_500_000, period="month"),
    "erase": None,
}


def parse(
    text: str, *, category: str | None = None, deal_type: str | None = None
) -> PriceFact | None:
    return FACTS.get(text)


def price_only(listing: Listing, text: str) -> ListingPatch:
    return derive(listing, text, derivations=(PriceDerivation(parse=parse),))


def never_by_verdict(listing: Listing, text: str) -> bool:
    """Правка не объясняется сменой стороны: пара не менялась или разбор ни при чём."""
    return False


def by_verdict_for(*ids: int) -> ByVerdict:
    """Правки этих карточек объясняются сменой пары: цену испортил вердикт модели."""

    def explained(listing: Listing, text: str) -> bool:
        return listing.id in ids

    return explained


def standard_rows() -> list[ListingWithText]:
    """Девять карточек, по одному исходу на каждую."""
    return [
        row(1, "fill"),  # filled
        row(2, "replace", price=5_500),  # replaced
        row(3, "disagree", price=7_000_000),  # disagreed
        row(4, "lost", price=9_000_000),  # lost
        row(5, "rate", category="motorbike"),  # rate_only
        row(6, "fill-sell", category="motorbike", deal_type="sell"),  # filled
        row(7, "same", price=12_500_000),  # same
        row(8, None),  # сырья нет
        row(9, "erase", price=5_000_000_000),  # erased
    ]


class Store:
    """Память вместо базы."""

    def __init__(
        self,
        rows: list[ListingWithText],
        *,
        stale: tuple[int, ...] = (),
        failing: tuple[int, ...] = (),
    ) -> None:
        self.rows = {r.listing.id: r for r in rows}
        self.stale, self.failing = set(stale), set(failing)
        self.asked: list[tuple[int, int]] = []
        self.writes: list[list[int | None]] = []

    async def page(self, after_id: int, limit: int) -> list[ListingWithText]:
        self.asked.append((after_id, limit))
        wanted = sorted(i for i in self.rows if i is not None and i > after_id)[:limit]
        return [self.rows[i] for i in wanted]

    async def write(self, items: list[Item]) -> list[WriteResult]:
        self.writes.append([item.listing.id for item, _ in items])
        results = []
        for item, patch in items:
            if item.listing.id in self.failing:
                results.append(WriteResult(WriteStatus.FAILED, "DataError"))
            elif item.listing.id in self.stale:
                results.append(WriteResult(WriteStatus.STALE))
            else:
                self.rows[item.listing.id] = ListingWithText(
                    patch.applied_to(item.listing), item.text
                )
                results.append(WriteResult(WriteStatus.APPLIED))
        return results

    def snapshot(self) -> dict[int | None, Listing]:
        return {i: r.listing for i, r in self.rows.items()}


async def run(
    store: Store,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    since_id: int = 0,
    size: int = 3,
    derive_row: Derive = price_only,
    by_verdict: ByVerdict = never_by_verdict,
) -> EnrichReport:
    report = EnrichReport()
    enrich_pass = EnrichPass(
        page=store.page,
        write=store.write,
        derive_row=derive_row,
        by_verdict=by_verdict,
        size=size,
    )
    await enrich_pass.run(report, dry_run=dry_run, limit=limit, since_id=since_id)
    return report


# ── курсор, пачки, пределы ─────────────────────────────────────────────────


async def test_the_pass_walks_the_pages_by_cursor_and_stops_on_an_empty_one() -> None:
    store = Store(standard_rows())

    report = await run(store, size=3)

    assert store.asked == [(0, 3), (3, 3), (6, 3), (9, 3)]
    assert report.last_id == 9 and report.stop_reason == END
    assert report.totals()["seen"] == 9


async def test_a_limit_caps_the_cards_and_shrinks_the_last_page() -> None:
    store = Store(standard_rows())

    report = await run(store, limit=5, size=3)

    assert store.asked == [(0, 3), (3, 2)]
    assert report.totals()["seen"] == 5
    assert report.last_id == 5 and report.stop_reason == LIMIT


async def test_the_pass_can_start_after_a_given_id() -> None:
    store = Store(standard_rows())

    report = await run(store, since_id=6, size=3)

    assert store.asked[0] == (6, 3)
    assert report.totals()["seen"] == 3, "карточки 7, 8 и 9"


async def test_a_pass_over_nothing_is_an_empty_report_not_an_error() -> None:
    report = await run(Store([]))

    assert report.totals()["seen"] == 0 and report.stop_reason == END


# ── сухой прогон и боевой ──────────────────────────────────────────────────


async def test_a_dry_run_never_writes_and_leaves_everything_as_it_was() -> None:
    store = Store(standard_rows())
    before = store.snapshot()

    async def forbidden(_items: list[Item]) -> list[WriteResult]:
        raise AssertionError("сухой прогон не пишет")

    report = EnrichReport()
    await EnrichPass(page=store.page, write=forbidden, derive_row=price_only, size=3).run(
        report, dry_run=True
    )

    assert store.snapshot() == before
    assert report.dry_run is True
    assert report.totals()["would_write"] == 5 and report.totals()["written"] == 0


async def test_a_live_pass_writes_only_the_cards_that_have_something_to_write() -> None:
    store = Store(standard_rows())

    report = await run(store, size=10)

    assert store.writes == [[1, 2, 5, 6, 9]], "3, 4, 7 — исход без патча; 8 — нет текста"
    assert report.totals()["written"] == 5 and report.totals()["would_write"] == 0


async def test_the_numbers_of_the_report_are_the_cases_of_the_batch() -> None:
    report = await run(Store(standard_rows()), size=3)

    totals = report.totals()
    assert {k: totals[k] for k in totals} == {
        "seen": 9,
        "written": 5,
        "price.filled": 2,
        "price.replaced": 1,
        "price.erased": 1,
        "price.disagreed": 1,
        "price.lost": 1,
        "price.rate_only": 1,
        "price.same": 1,
        "skip.no_text": 1,
    }
    assert report.scopes[("motorbike", "rent_out")]["price.rate_only"] == 1
    assert report.scopes[("motorbike", "sell")]["price.filled"] == 1
    assert report.scopes[("apartment", "rent_out")]["price.replaced"] == 1
    assert report.scopes[("apartment", "rent_out")]["price.erased"] == 1


async def test_every_seen_card_is_an_outcome_or_a_skip_never_both_never_neither() -> None:
    store = Store(standard_rows(), stale=(2,), failing=(5,))

    report = await run(store, size=3, by_verdict=by_verdict_for(1, 2, 9))

    for counter in report.scopes.values():
        accounted = sum(n for k, n in counter.items() if k.startswith(("price.", "skip.")))
        assert accounted == counter["seen"], "пометка after_verdict в счёт не входит"
    assert report.totals()["seen"] == 9


async def test_the_second_pass_changes_nothing_and_writes_nothing() -> None:
    store = Store(standard_rows())
    await run(store, size=3)
    after_first = store.snapshot()
    store.writes.clear()

    second = await run(store, size=3)

    assert store.writes == [], "повторный проход в базу не ходит"
    assert store.snapshot() == after_first
    assert second.totals()["written"] == 0
    for fresh in ("price.filled", "price.replaced", "price.erased"):
        assert second.totals()[fresh] == 0, fresh


# ── пометка «после смены стороны или категории» ────────────────────────────


async def test_a_price_filled_after_a_change_of_side_is_marked() -> None:
    report = await run(Store(standard_rows()), by_verdict=by_verdict_for(1))

    assert report.totals()["after_verdict.filled"] == 1, "карточка 1; карточка 6 не менялась"
    assert report.totals()["price.filled"] == 2


async def test_replaced_and_erased_prices_are_marked_too() -> None:
    report = await run(Store(standard_rows()), by_verdict=by_verdict_for(2, 9))

    assert report.totals()["after_verdict.replaced"] == 1
    assert report.totals()["after_verdict.erased"] == 1
    assert report.totals()["after_verdict.filled"] == 0


async def test_a_card_whose_side_never_changed_is_not_marked() -> None:
    report = await run(Store(standard_rows()), by_verdict=never_by_verdict)

    assert not [key for key in report.totals() if key.startswith("after_verdict.")]


async def test_only_outcomes_that_change_a_column_ask_for_the_mark() -> None:
    asked: list[int | None] = []

    def spy(listing: Listing, text: str) -> bool:
        asked.append(listing.id)
        return False

    await run(Store(standard_rows()), by_verdict=spy)

    assert sorted(i for i in asked if i is not None) == [1, 2, 6, 9], "filled, replaced, erased"


async def test_the_mark_goes_only_to_cards_that_were_really_written() -> None:
    store = Store(standard_rows(), stale=(1,))

    report = await run(store, by_verdict=by_verdict_for(1, 2))

    assert report.totals()["after_verdict.filled"] == 0, "карточка 1 не записалась"
    assert report.totals()["after_verdict.replaced"] == 1


async def test_a_dry_run_marks_too() -> None:
    report = await run(Store(standard_rows()), dry_run=True, by_verdict=by_verdict_for(1))

    assert report.totals()["after_verdict.filled"] == 1


async def test_a_check_that_fails_costs_the_mark_not_the_card() -> None:
    def broken(listing: Listing, text: str) -> bool:
        if listing.id == 1:
            raise ValueError("не разобрать: +84 90 123 45 67")
        return False

    store = Store(standard_rows())
    with capture_logs() as logs:
        report = await run(store, by_verdict=broken)

    assert report.totals()["after_verdict.unknown"] == 1
    assert report.totals()["written"] == 5, "карточка 1 записана, как и остальные"
    assert "+84" not in repr(logs), "текст чужого сбоя в лог не попадает"


# ── пропуски ───────────────────────────────────────────────────────────────


async def test_a_card_without_source_text_is_a_named_skip() -> None:
    report = await run(Store([row(8, None)]))

    assert report.totals()["skip.no_text"] == 1
    assert report.samples["skip.no_text"] == [8]


async def test_a_card_changed_since_the_read_is_skipped_and_not_counted_as_filled() -> None:
    store = Store(standard_rows(), stale=(2,))

    report = await run(store, size=10)

    assert report.totals()["skip.changed_since_read"] == 1
    assert report.totals()["price.replaced"] == 0, "заменено — только то, что записалось"
    assert report.totals()["written"] == 4


async def test_a_card_the_database_refused_is_skipped_and_the_others_go_on() -> None:
    store = Store(standard_rows(), failing=(1,))

    report = await run(store, size=10)

    assert report.totals()["skip.write_error"] == 1
    assert report.totals()["written"] == 4
    assert report.totals()["price.filled"] == 1, "карточка 6; 1-я не записалась"


async def test_a_derivation_that_fails_on_one_card_does_not_stop_the_batch() -> None:
    def flaky(listing: Listing, text: str) -> ListingPatch:
        if listing.id == 3:
            raise DerivationFailed("price")
        if listing.id == 4:
            raise ValueError("не разобрать +84 90 123 45 67")
        return ListingPatch(outcomes=("price.same",))

    report = await run(Store(standard_rows()), size=10, derive_row=flaky)

    assert report.totals()["skip.derive_error.price"] == 1
    assert report.totals()["skip.derive_error"] == 1
    assert report.totals()["price.same"] == 6, "остальные просмотрены"
    assert report.totals()["seen"] == 9


async def test_a_batch_that_fails_to_write_is_counted_raised_and_remembered() -> None:
    store = Store(standard_rows())

    async def broken(_items: list[Item]) -> list[WriteResult]:
        raise RuntimeError("база пропала")

    report = EnrichReport()
    with pytest.raises(RuntimeError):
        await EnrichPass(page=store.page, write=broken, derive_row=price_only, size=3).run(report)

    assert report.totals()["skip.batch_failed"] == 2, "в первой пачке (1-3) патч у 1 и 2"
    assert report.stop_reason == "", "не дошли до конца: об этом скажет отчёт"
    assert report.last_id == 0, "курсор не ушёл за пачку, которая не записалась"


async def test_a_writer_that_answers_for_fewer_cards_than_asked_is_an_error() -> None:
    """Молча посчитать «сколько ответили» значило бы потерять карточки из отчёта."""
    store = Store(standard_rows())

    async def forgetful(_items: list[Item]) -> list[WriteResult]:
        return []

    report = EnrichReport()
    with pytest.raises(ValueError):
        await EnrichPass(page=store.page, write=forgetful, derive_row=price_only, size=3).run(
            report
        )


async def test_an_interrupt_is_not_swallowed_the_batch_is_counted_and_the_report_logged() -> None:
    store = Store(standard_rows())

    async def interrupted(_items: list[Item]) -> list[WriteResult]:
        raise KeyboardInterrupt

    report = EnrichReport()
    with capture_logs() as logs, pytest.raises(KeyboardInterrupt):
        await EnrichPass(page=store.page, write=interrupted, derive_row=price_only, size=3).run(
            report
        )

    final = [entry for entry in logs if entry["event"] == "enrich.report"]
    assert len(final) == 1 and final[0]["stop_reason"] == ""
    assert report.totals()["skip.batch_failed"] == 2, "и при прерывании отчёт сходится по счёту"


async def test_row_warnings_are_capped_but_every_failure_is_still_counted() -> None:
    """Баг вывода на каждой карточке дал бы 18 тысяч одинаковых строк лога."""

    def always(listing: Listing, text: str) -> ListingPatch:
        raise ValueError("не разобрать")

    rows = [row(n, "x") for n in range(1, MAX_ROW_WARNINGS + 11)]
    with capture_logs() as logs:
        report = await run(Store(rows), size=10, derive_row=always)

    warnings = [entry for entry in logs if entry["event"] == "enrich.row_failed"]
    assert len(warnings) == MAX_ROW_WARNINGS
    assert report.totals()["skip.derive_error"] == MAX_ROW_WARNINGS + 10, "в отчёте — все"


# ── что попадает в лог ─────────────────────────────────────────────────────

PHONE, NICK = "+84 90 123 45 67", "@seller_nick"


async def test_logs_and_report_carry_numbers_and_ids_never_texts_or_contacts() -> None:
    rows = [
        row(1, f"Сдам квартиру, {PHONE} {NICK}\nАрендная плата: 12.5 млн VND / месяц"),
        row(2, f"Сдам студию, {PHONE} {NICK}"),
    ]

    def flaky(listing: Listing, text: str) -> ListingPatch:
        if listing.id == 2:
            raise ValueError(f"не разобрать: {text}")
        return ListingPatch(outcomes=("price.same",))

    store = Store(rows)
    report = EnrichReport()
    with capture_logs() as logs:
        await EnrichPass(page=store.page, write=store.write, derive_row=flaky).run(report)

    everything = repr(logs) + report.render() + repr(report.fields())
    assert PHONE not in everything and NICK not in everything
    assert "Сдам" not in everything, "ни куска объявления"
    assert any(entry["event"] == "enrich.report" for entry in logs)
    assert any(entry["event"] == "enrich.batch" for entry in logs)


async def test_the_final_report_event_is_the_structured_report() -> None:
    store = Store(standard_rows())
    report = EnrichReport()

    with capture_logs() as logs:
        await EnrichPass(
            page=store.page,
            write=store.write,
            derive_row=price_only,
            by_verdict=never_by_verdict,
            size=4,
        ).run(report, dry_run=True)

    (final,) = [entry for entry in logs if entry["event"] == "enrich.report"]
    assert final["dry_run"] is True and final["stop_reason"] == END
    assert final["totals"]["seen"] == 9
    assert final["scopes"]["apartment/rent_out"]["seen"] == 7


# ── запись каждой карточки изолирована ─────────────────────────────────────


class Applier:
    """Репозиторий в миниатюре: кто-то записывается, кто-то устарел, кто-то падает."""

    def __init__(self, *, stale: tuple[int, ...] = (), boom: tuple[int, ...] = ()) -> None:
        self.stale, self.boom = set(stale), set(boom)
        self.seen: list[int | None] = []

    async def apply(self, row: Listing, patch: ListingPatch) -> bool:
        self.seen.append(row.id)
        if row.id in self.boom:
            raise RuntimeError("отказ базы: INSERT … +84 90 123 45 67")
        return row.id not in self.stale


def planned(*ids: int) -> list[Item]:
    return [(row(i, "x"), ListingPatch({"lang": "ru"})) for i in ids]


async def test_each_write_is_isolated_one_refusal_does_not_cost_the_neighbours() -> None:
    applier = Applier(stale=(2,), boom=(3,))

    results = await write_each(applier, planned(1, 2, 3, 4))

    assert applier.seen == [1, 2, 3, 4], "после отказа идём дальше"
    assert [r.status for r in results] == [
        WriteStatus.APPLIED,
        WriteStatus.STALE,
        WriteStatus.FAILED,
        WriteStatus.APPLIED,
    ]


async def test_a_refusal_is_named_by_class_and_never_by_its_text() -> None:
    (result,) = await write_each(Applier(boom=(1,)), planned(1))

    assert result.error == "RuntimeError"
    assert "+84" not in result.error


class Interrupting:
    async def apply(self, row: Listing, patch: ListingPatch) -> bool:
        raise KeyboardInterrupt


async def test_an_interrupt_in_the_middle_of_a_batch_is_not_a_per_card_refusal() -> None:
    with pytest.raises(KeyboardInterrupt):
        await write_each(Interrupting(), planned(1, 2))
