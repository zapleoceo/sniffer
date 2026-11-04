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


def read_bedrooms(text: str) -> int | None:
    """Return an explicitly stated bedroom count, or unknown."""
    return next(
        (
            int(match.group(1))
            for match in _BEDROOM_RE.finditer(text)
            if 1 <= int(match.group(1)) <= 9
        ),
        None,
    )


def read_total_rooms(text: str) -> int | None:
    """Return explicitly stated total rooms without treating bedrooms as total."""
    return next(
        (
            int(match.group(1))
            for match in _TOTAL_ROOM_RE.finditer(text)
            if 1 <= int(match.group(1)) <= 9
        ),
        None,
    )
