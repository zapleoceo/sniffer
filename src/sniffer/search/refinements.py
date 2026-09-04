"""Conservative recognition of follow-up edits, without starting a new request."""

from __future__ import annotations

import re

from sniffer.domain.dialogue import apply_answer
from sniffer.domain.passport import Passport
from sniffer.search.intake_rules import detect_intent, parse_query

# Only a complete price phrase is an implicit edit. An unknown noun such as
# «лодку до 500» must not silently change the selected scooter request.
_PRICE_ONLY = re.compile(
    r"(?:теперь\s+|бюджет\s*:?\s*|лучше\s+)?(?:до\s*|не\s+дороже\s*|максимум\s*)?"
    r"[$€₫]?\s*\d[\d\s.,]*\s*(?:млн|миллион\w*|тыс\.?|k|tr)?\s*"
    r"(?:usd|vnd|eur|rub|доллар\w*|донг\w*|руб\w*|евро|[$€₫])?",
    re.IGNORECASE,
)


def price_refinement(current: Passport, text: str) -> Passport | None:
    """A standalone budget answer edits the selected request, not its identity."""
    if current.category is None or not _PRICE_ONLY.fullmatch(text.strip()):
        return None
    fresh = parse_query(text)
    if fresh.budget.max is None:
        return None
    revised = apply_answer(current, "budget.max", fresh.budget)
    return revised.model_copy(update={"raw_query": _history(current, text)})


def merge_edit(current: Passport, fresh: Passport) -> Passport:
    """An explicit edit preserves unspecified fields when the subject stays the same."""
    if (
        current.category is not None
        and fresh.category is not None
        and fresh.category != current.category
    ):
        return fresh
    attributes = dict(current.attributes)
    if fresh.attributes.get("brand") not in (None, current.attributes.get("brand")):
        attributes.pop("model", None)
    intent = (
        detect_intent(fresh.raw_query)
        or (detect_intent(current.raw_query) if current.category is None else current.intent)
        or fresh.intent
    )
    revised = current.model_copy(
        update={
            "intent": intent,
            "city": fresh.city or current.city,
            "category": fresh.category or current.category,
            "attributes": {**attributes, **fresh.attributes},
            "raw_query": fresh.raw_query,
        }
    )
    if fresh.budget.max is not None:
        revised = apply_answer(revised, "budget.max", fresh.budget)
    if intent != current.intent:
        revised = revised.model_copy(
            update={"budget": revised.budget.model_copy(update={"period": fresh.budget.period})}
        )
    return revised


def _history(current: Passport, text: str) -> str:
    original = current.raw_query.split("\nПоследнее уточнение", 1)[0][:300]
    return f"{original}\nПоследнее уточнение (заменяет прежние условия): {text[:150]}"
