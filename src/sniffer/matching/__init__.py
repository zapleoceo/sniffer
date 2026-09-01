"""Подбор карточек по паспорту: условия отбора и оценка находки.

SQL сюда не попадает — он живёт в `db/repositories/listings.py`. Здесь
только решение, что считать подходящим и насколько.
"""

from __future__ import annotations

from sniffer.matching.rules import (
    MATCH_MAX_AGE_DAYS,
    MATCH_MIN_SCORE,
    filter_for,
    score,
    worth_sending,
)

__all__ = [
    "MATCH_MAX_AGE_DAYS",
    "MATCH_MIN_SCORE",
    "filter_for",
    "score",
    "worth_sending",
]
