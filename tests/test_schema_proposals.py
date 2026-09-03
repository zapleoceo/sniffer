"""Agents can suggest bounded data; they cannot carry or execute migrations."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from sniffer.domain.proposals import SchemaProposal


def test_proposal_is_review_only_typed_data() -> None:
    proposal = SchemaProposal(
        kind="attribute",
        name="battery_range",
        description="Observed range information in electric scooter listings.",
        value_type="number",
    )
    assert proposal.model_dump() == {
        "kind": "attribute",
        "name": "battery_range",
        "description": "Observed range information in electric scooter listings.",
        "value_type": "number",
    }


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "migration"},
        {"name": "DROP TABLE users"},
        {"name": "$where"},
        {"value_type": "sql"},
        {"description": "short"},
        {"sql": "ALTER TABLE users"},
    ],
)
def test_executable_or_unbounded_proposal_payload_is_rejected(change: dict[str, Any]) -> None:
    data: dict[str, Any] = {
        "kind": "schema",
        "name": "vehicle_range",
        "description": "A developer must review this candidate first.",
        "value_type": "review_required",
        **change,
    }
    with pytest.raises(ValidationError):
        SchemaProposal.model_validate(data)


def test_no_agent_repository_exposes_approval_or_execution() -> None:
    from sniffer.db.repositories.schema_proposals import SchemaProposalRepository

    public = {name for name in dir(SchemaProposalRepository) if not name.startswith("_")}
    assert public == {"for_request", "for_task"}
