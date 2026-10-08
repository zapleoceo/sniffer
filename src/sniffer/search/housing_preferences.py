"""Keep current, unverified housing wishes separate from archive hard filters."""

from __future__ import annotations

import re
from collections.abc import Mapping

from sniffer.domain.passport import HOUSING_PREFERENCES_KEY, Category

HOUSING = frozenset({Category.APARTMENT, Category.ROOM, Category.HOUSE})
KEY = HOUSING_PREFERENCES_KEY

_PATTERNS = {
    "location": re.compile(r"\b(?:ближе|рядом|недалеко|север|юг|восток|запад|у моря|near)\b", re.I),
    "kitchen": re.compile(r"\bкухн\w*\b", re.I),
    "renovation": re.compile(r"\bремонт\w*\b", re.I),
    "appliances": re.compile(r"\bтехник\w*\b", re.I),
    "move_in": re.compile(r"\bзаезд\w*\b", re.I),
    "term": re.compile(r"\bсрок\s+аренды\b", re.I),
}
_CANCEL = re.compile(r"\b(?:без|не\s+нужн\w*|больше\s+не|отмен\w*)\b", re.I)


def updates(query: str, category: Category | None) -> dict[str, str | None]:
    """Extract raw clauses; ``None`` cancels a previous wish of the same kind."""
    if category not in HOUSING:
        return {}
    found: dict[str, str | None] = {}
    for sentence in re.split(r"(?<=[.!?])\s+", query.strip()):
        for clause in sentence.split(","):
            clause = clause.strip(" .")
            for kind, pattern in _PATTERNS.items():
                if pattern.search(clause):
                    if _CANCEL.search(clause):
                        found[kind] = None
                    elif kind == "location" and found.get(kind):
                        found[kind] = f"{found[kind]}; {clause}"
                    else:
                        found[kind] = clause
    return found


def current(attributes: Mapping[str, object]) -> dict[str, str]:
    value = attributes.get(KEY)
    if not isinstance(value, dict):
        return {}
    return {str(key): text for key, text in value.items() if isinstance(text, str)}


def effective_query(raw_query: str, attributes: Mapping[str, object]) -> str:
    """Give planning only active wishes and the latest edit, keeping history for audit."""
    if "\nLatest follow-up:" not in raw_query:
        return raw_query
    latest = raw_query.rsplit("\nLatest follow-up:", 1)[1].strip()
    active_clauses = [
        clause.strip()
        for clause in latest.split(",")
        if not (
            _CANCEL.search(clause) and any(pattern.search(clause) for pattern in _PATTERNS.values())
        )
    ]
    latest = ", ".join(clause for clause in active_clauses if clause)
    wishes = "; ".join(current(attributes).values())
    return f"{wishes}. {latest}" if wishes else latest
