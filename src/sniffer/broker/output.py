"""Local validation of untrusted structured model output; no repair or I/O."""

from __future__ import annotations

import json
import math
from typing import Any, Literal

from jsonschema import ValidationError
from jsonschema.validators import validator_for

OutputReason = Literal[
    "invalid_json",
    "duplicate_key",
    "nonfinite_number",
    "not_object",
    "schema_mismatch",
    "incomplete",
    "refusal",
]


class InvalidOutput(ValueError):
    def __init__(self, reason: OutputReason) -> None:
        self.reason = reason
        super().__init__(reason)


def check_schema(schema: dict[str, Any]) -> None:
    """Fail on a programmer's invalid schema before buying a model response."""
    validator_for(schema).check_schema(schema)


def parse_object(text: str, schema: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_nonfinite)
    except InvalidOutput:
        raise
    except (ValueError, TypeError, RecursionError) as exc:
        raise InvalidOutput("invalid_json") from exc
    if not isinstance(parsed, dict):
        raise InvalidOutput("not_object")
    try:
        _finite(parsed)
        validator_for(schema)(schema).validate(parsed)
    except ValidationError as exc:
        raise InvalidOutput("schema_mismatch") from exc
    except RecursionError as exc:
        raise InvalidOutput("invalid_json") from exc
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidOutput("duplicate_key")
        result[key] = value
    return result


def _nonfinite(value: str) -> Any:
    raise InvalidOutput("nonfinite_number")


def _finite(value: Any) -> None:
    # JSON's 1e999 is syntactically valid, but Python converts it to infinity.
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidOutput("nonfinite_number")
    if isinstance(value, dict):
        for item in value.values():
            _finite(item)
    elif isinstance(value, list):
        for item in value:
            _finite(item)
