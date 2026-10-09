"""Классификация причин отказа: временный / постоянный / неизвестно.

Страница «База» показывает владельцу, вернётся ли отклонённый кандидат. Ошибка
в словаре здесь — это ложное «навсегда» про чат, который просто упал по сбою.
"""

from __future__ import annotations

import pytest

from sniffer.domain import reject_reasons
from sniffer.domain.reject_reasons import RejectClass
from sniffer.sources import telegram_discover_reference as reference


def _written_reasons() -> set[str]:
    return {
        value
        for name, value in vars(reference).items()
        if name.startswith("REJECT_") and isinstance(value, str)
    }


def test_repeated_unknown_outcomes_are_temporary() -> None:
    assert reject_reasons.classify("too_many_attempts") is RejectClass.TEMPORARY
    assert reject_reasons.class_label("too_many_attempts") == "временный"


@pytest.mark.parametrize("reason", ["unresolved", "city_unknown"])
def test_ambiguous_records_are_unknown_not_temporary(reason: str) -> None:
    """Старый `unresolved` писался и на «нет такого чата», и на сбой сети."""
    assert reject_reasons.classify(reason) is RejectClass.UNKNOWN


@pytest.mark.parametrize(
    ("reason", "kind"),
    [
        ("already_member", RejectClass.MEMBERSHIP),
        ("already_inside", RejectClass.MEMBERSHIP),
        ("join_request_sent", RejectClass.PENDING),
        ("request_needed", RejectClass.PENDING),
    ],
)
def test_states_are_not_called_rejections(reason: str, kind: RejectClass) -> None:
    assert reject_reasons.classify(reason) is kind


@pytest.mark.parametrize(
    "reason",
    [
        "user",
        "channel",
        "bot",
        "foreign_city",
        "join_refused",
    ],
)
def test_verdicts_about_the_chat_are_permanent(reason: str) -> None:
    assert reject_reasons.classify(reason) is RejectClass.PERMANENT
    assert reject_reasons.class_label(reason) == "постоянный"


def test_an_unknown_reason_is_never_called_permanent() -> None:
    """«Постоянный» — утверждение, что кандидат не вернётся; про чужой код его нет."""
    assert reject_reasons.classify("brand_new_reason") is RejectClass.UNKNOWN
    assert reject_reasons.class_label("brand_new_reason") == "неизвестно"
    # Код остаётся видимым, иначе новую причину нечем искать в логах.
    assert reject_reasons.label("brand_new_reason") == "brand_new_reason"


def test_every_reason_the_scout_writes_is_classified() -> None:
    """Новый `REJECT_*` без записи в словаре попадал бы на страницу как «неизвестно»."""
    written = _written_reasons()
    assert written, "константы причин не найдены — тест ничего не проверяет"
    assert written <= set(reject_reasons.REASONS), written - set(reject_reasons.REASONS)


def test_totals_by_class_add_up() -> None:
    counts = {
        "user": 5,
        "unresolved": 2,
        "too_many_attempts": 1,
        "weird": 4,
        "already_member": 7,
    }
    by_class = reject_reasons.totals_by_class(counts)

    assert by_class[RejectClass.PERMANENT] == 5
    assert by_class[RejectClass.TEMPORARY] == 1
    assert by_class[RejectClass.UNKNOWN] == 6
    assert by_class[RejectClass.MEMBERSHIP] == 7
    assert sum(by_class.values()) == sum(counts.values())
