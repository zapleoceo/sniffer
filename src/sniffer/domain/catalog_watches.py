"""Public filters plus a bounded multi-district selector for one personal watch."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from sniffer.domain.districts import PLACE_BY_SLUG
from sniffer.domain.public_catalog import Filters, Slug


class WatchFilters(Filters):
    source_reference_only: Literal[True] = True
    districts: Annotated[list[Slug], Field(max_length=12)] | None = None

    @model_validator(mode="after")
    def check_districts(self) -> Self:
        if self.districts is None:
            return self
        if self.district is not None:
            raise ValueError("Use district or districts, not both.")
        if not self.districts:
            raise ValueError("Provide at least one district or omit districts.")
        for name in self.districts:
            place = PLACE_BY_SLUG.get(name)
            if place is None or place.city != self.city:
                raise ValueError("Unknown district for this city; use catalog_guide.")
        self.districts = sorted(set(self.districts))
        return self
