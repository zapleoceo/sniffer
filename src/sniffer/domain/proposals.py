"""Suggestions are data for human review, never executable migrations."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SchemaProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    kind: Literal["attribute", "source", "schema"]
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    description: str = Field(min_length=10, max_length=1500)
    value_type: Literal["string", "number", "boolean", "review_required"]
