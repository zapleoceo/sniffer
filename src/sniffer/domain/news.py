"""News editorial data contracts, independent of listings and passports.

No runtime path creates these records yet. They make provenance and uncertainty
explicit before a future collector or publisher is connected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class MediaRights(StrEnum):
    UNKNOWN = "unknown"
    OWNED = "owned"
    LICENSED = "licensed"
    LINK_ONLY = "link_only"


@dataclass(frozen=True, slots=True)
class NewsOriginal:
    raw_message_id: int
    source_url: str
    observed_at: datetime
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class Evidence:
    original: NewsOriginal
    excerpt: str


@dataclass(frozen=True, slots=True)
class Claim:
    statement: str
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True, slots=True)
class NewsDraft:
    claims: tuple[Claim, ...]
    independent_source_ids: frozenset[str]
    media_rights: MediaRights = MediaRights.UNKNOWN
