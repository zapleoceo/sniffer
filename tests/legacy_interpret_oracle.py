"""Прежний `interpret` (цепочка `if`), снятый дословно до перехода на таблицу читателей."""

from __future__ import annotations

from sniffer.domain.dialogue import AnswerValue
from sniffer.search.answers import _CONDITION_RULES, _match, _rooms
from sniffer.search.budget_rules import parse_budget
from sniffer.search.intake_rules import (
    detect_brand,
    detect_category,
    detect_city,
    detect_transmission,
    parse_query,
)


def legacy_interpret(field: str, text: str) -> AnswerValue | None:
    """Слова клиента → значение поля. `None` — «это не ответ на вопрос».

    `None` важнее, чем кажется: на вопрос про бюджет клиент нередко отвечает
    новым запросом («ладно, тогда квартиру»), и принять его за сумму значит
    потерять запрос.
    """
    if field == "budget.max":
        budget = parse_budget(text)
        return budget if budget.max else None
    if field == "category":
        parsed = parse_query(text)
        if parsed.attributes.get("body_type") == "tay_ga":
            return "scooter"
        category = detect_category(text)
        return category.value if category else None
    if field == "city":
        return detect_city(text)
    if field == "attributes.brand":
        return detect_brand(text)
    if field == "attributes.transmission":
        return detect_transmission(text)
    if field == "attributes.condition":
        return _match(_CONDITION_RULES, text)
    if field == "attributes.rooms":
        return _rooms(text)
    return None
