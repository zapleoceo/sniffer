"""Отчёт прохода догона: счётчики в разрезе (категория, сторона), без текстов.

Отчёт читает человек перед боевым прогоном и решает по нему, запускать ли.
Поэтому он обязан отвечать на пять вопросов (просмотрено, заполнено, заменено,
расхождений, потеряно) и на шестой — «а что пропустили и почему».
"""

from __future__ import annotations

import json

from sniffer.domain.records import Listing
from sniffer.pipeline import enrich_price as price
from sniffer.worker.enrich_report import (
    AFTER_VERDICT,
    ERASED_LISTED,
    REVIEW_UP_TO,
    SAMPLES_PER_KEY,
    EnrichReport,
)
from tests.enrich_support import card


def apartment(listing_id: int = 1) -> Listing:
    return card(listing_id)


def bike(listing_id: int = 2) -> Listing:
    return card(listing_id, category="motorbike", deal_type="sell")


def test_counters_are_kept_apart_by_category_and_side_of_the_deal() -> None:
    report = EnrichReport()

    report.seen(apartment())
    report.seen(bike())
    report.seen(bike(3))
    report.outcomes(bike(), [price.FILLED])

    assert report.scopes[("apartment", "rent_out")]["seen"] == 1
    assert report.scopes[("motorbike", "sell")]["seen"] == 2
    assert report.scopes[("motorbike", "sell")][price.FILLED] == 1
    assert ("apartment", "rent_out") in report.scopes and len(report.scopes) == 2


def test_totals_add_up_the_scopes() -> None:
    report = EnrichReport()
    for listing in (apartment(1), apartment(2), bike(3)):
        report.seen(listing)
        report.outcomes(listing, [price.SAME])

    assert report.totals()["seen"] == 3
    assert report.totals()[price.SAME] == 3


def test_a_write_is_a_write_live_and_a_would_write_in_a_dry_run() -> None:
    live, dry = EnrichReport(dry_run=False), EnrichReport(dry_run=True)

    live.wrote(apartment())
    dry.wrote(apartment())

    assert live.totals()["written"] == 1 and live.totals()["would_write"] == 0
    assert dry.totals()["would_write"] == 1 and dry.totals()["written"] == 0


def test_skips_are_counted_by_reason_and_summed() -> None:
    report = EnrichReport()

    report.seen(apartment(1))
    report.outcomes(apartment(2), [price.SAME])
    report.wrote(apartment(2))
    report.skip(apartment(1), "no_text")
    report.skip(apartment(2), "no_text")
    report.skip(bike(3), "changed_since_read")

    assert report.totals()["skip.no_text"] == 2
    assert report.totals()["skip.changed_since_read"] == 1
    assert report.skipped() == 3, "только пропуски: просмотренное, исходы и записанное не в счёте"


def test_a_few_ids_of_each_outcome_are_remembered_for_a_human_to_look_at() -> None:
    report = EnrichReport()

    for listing_id in range(1, SAMPLES_PER_KEY + 6):
        report.outcomes(apartment(listing_id), [price.REPLACED])

    assert report.samples[price.REPLACED] == list(range(1, SAMPLES_PER_KEY + 1)), "первые, а не все"


def test_the_rendering_says_loudly_whether_anything_was_written() -> None:
    dry = EnrichReport(dry_run=True).render()
    live = EnrichReport(dry_run=False).render()

    assert "dry-run" in dry and "НИЧЕГО не записано" in dry
    assert "dry-run" not in live and "записано в базу" in live


def test_the_summary_answers_the_questions_of_the_owner() -> None:
    report = EnrichReport(dry_run=True)
    plan = {
        price.FILLED: 7,
        price.REPLACED: 2,
        price.ERASED: 3,
        price.DISAGREED: 4,
        price.LOST: 5,
    }
    listing_id = 0
    for outcome, how_many in plan.items():
        for _ in range(how_many):
            listing_id += 1
            report.seen(apartment(listing_id))
            report.outcomes(apartment(listing_id), [outcome])

    text = report.render()

    assert "просмотрено 21" in text
    assert "заполнено 7" in text
    assert "заменено 2" in text
    assert "стёрто 3" in text
    assert "расхождений 4" in text
    assert "потеряно 5" in text


def test_the_summary_says_how_many_of_them_are_explained_by_a_change_of_side_or_category() -> None:
    """Вердикт модели приходит после извлечения: эти карточки читались под другой стороной."""
    report = EnrichReport(dry_run=True)
    marks = {"filled": 2, "replaced": 1, "erased": 3}
    for name, how_many in marks.items():
        for n in range(how_many):
            report.outcomes(apartment(100 + n), [f"{AFTER_VERDICT}{name}"])

    line = "Из них объясняются сменой стороны или категории после вердикта: "
    assert line + "заполнено 2 · заменено 1 · стёрто 3" in report.render()


def test_ids_are_shown_only_for_outcomes_rare_enough_to_review_by_hand() -> None:
    report = EnrichReport()
    for listing_id in range(1, REVIEW_UP_TO + 1):
        report.outcomes(apartment(listing_id), [price.REPLACED])
    for listing_id in range(1, REVIEW_UP_TO + 2):
        report.outcomes(apartment(listing_id), [price.FILLED])

    lines: dict[str, str] = {}
    for line in report.render().splitlines():
        if "price." in line:
            lines.setdefault(line.split()[0], line)  # первая строка ключа — итоговая

    assert "id:" in lines[price.REPLACED], "ровно столько, сколько ещё можно разобрать руками"
    assert "id:" not in lines[price.FILLED], "на одну больше — массовый исход, глазами не разбирают"


def test_every_scope_gets_its_own_section() -> None:
    report = EnrichReport()
    report.seen(apartment())
    report.seen(bike())

    text = report.render()

    assert "apartment / rent_out" in text and "motorbike / sell" in text


def test_the_structured_fields_are_plain_json_with_no_text_in_them() -> None:
    report = EnrichReport(dry_run=True, last_id=42, stop_reason="end")
    report.seen(apartment())
    report.outcomes(apartment(), [price.FILLED])

    payload = json.loads(json.dumps(report.fields()))

    assert payload["dry_run"] is True and payload["last_id"] == 42
    assert payload["totals"][price.FILLED] == 1
    assert payload["scopes"]["apartment/rent_out"]["seen"] == 1
    assert payload["samples"][price.FILLED] == [1]


def test_a_report_without_erased_prices_has_no_erased_section() -> None:
    report = EnrichReport(dry_run=True)
    report.seen(apartment())

    assert "id → прежняя сумма" not in report.render()


def test_the_erased_section_lists_id_and_old_amount_and_nothing_else() -> None:
    report = EnrichReport(dry_run=True)
    report.erased_price(card(7, price=5_000_000_000))

    lines = report.render().splitlines()
    at = lines.index("Цены, которые стёрли бы (id → прежняя сумма):")
    assert lines[at + 1] == "  7 → 5000000000"
    assert report.fields()["erased_prices"] == {7: "5000000000"}


def test_a_live_report_speaks_in_the_past_tense() -> None:
    report = EnrichReport(dry_run=False)
    report.erased_price(card(7, price=5_000_000_000))

    assert "Цены, которые стёрто (id → прежняя сумма):" not in report.render()
    assert "Цены, которые стёрто" not in report.render()
    assert "Стёртые цены (id → прежняя сумма):" in report.render()


def test_a_long_erased_list_is_cut_and_the_rest_is_counted() -> None:
    report = EnrichReport(dry_run=True)
    for number in range(1, ERASED_LISTED + 6):
        report.erased_price(card(number, price=5_000_000_000))

    text = report.render()
    assert f"  {ERASED_LISTED} → " in text and f"  {ERASED_LISTED + 1} → " not in text
    assert "… и ещё 5" in text


def test_a_card_without_a_price_or_id_is_not_listed_as_erased() -> None:
    report = EnrichReport()
    report.erased_price(card(8))

    assert report.erased == {}
