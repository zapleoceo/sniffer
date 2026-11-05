"""Explicit bedroom and total-room facts from housing text."""

from __future__ import annotations

import re

# Bedrooms are a separate housing fact. A living room is not a bedroom, and
# Vietnamese ads commonly abbreviate phòng ngủ as PN.
_BEDROOM_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:спальн\w*|bedrooms?\b|br\b|"
    r"ph[oò]ng\s*ng[uủ]\b|pn\b)",
    re.IGNORECASE,
)
_TOTAL_ROOM_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:комнат\w*|rooms?\b)(?!\s*(?:ng[uủ]|sleep))",
    re.IGNORECASE,
)
SPELLED_ROOM_NUMBERS = {
    "одна": 1,
    "одну": 1,
    "один": 1,
    "две": 2,
    "два": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
}
_SPELLED_TOTAL_ROOM_RE = re.compile(
    r"\b(" + "|".join(SPELLED_ROOM_NUMBERS) + r")\s+комнат\w*\b",
    re.IGNORECASE,
)
_SPELLED_BEDROOM_RE = re.compile(
    r"\b(" + "|".join(SPELLED_ROOM_NUMBERS) + r")\s+спал(?:ьн|ен)\w*\b",
    re.IGNORECASE,
)


def read_bedrooms(text: str) -> int | None:
    """Return an explicitly stated bedroom count, or unknown."""
    numbered = next(
        (
            int(match.group(1))
            for match in _BEDROOM_RE.finditer(text)
            if 1 <= int(match.group(1)) <= 9
        ),
        None,
    )
    if numbered is not None:
        return numbered
    return next(
        (
            SPELLED_ROOM_NUMBERS[match.group(1).lower()]
            for match in _SPELLED_BEDROOM_RE.finditer(text)
        ),
        None,
    )


def read_total_rooms(text: str) -> int | None:
    """Return explicitly stated total rooms without treating bedrooms as total."""
    numbered = next(
        (
            int(match.group(1))
            for match in _TOTAL_ROOM_RE.finditer(text)
            if 1 <= int(match.group(1)) <= 9
        ),
        None,
    )
    if numbered is not None:
        return numbered
    return next(
        (
            SPELLED_ROOM_NUMBERS[match.group(1).lower()]
            for match in _SPELLED_TOTAL_ROOM_RE.finditer(text)
        ),
        None,
    )
