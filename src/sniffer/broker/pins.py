"""Какую модель брокера закрепить за ролью.

Роль определяется по `schema_name` структурного вызова, а не новым аргументом:
у каждой роли своя схема, и так не меняются ни сигнатуры вызывающих, ни их
подделки в тестах. Цена — связь по строке; её держит
`tests/test_broker_pins.py`, сверяя таблицу с именами схем в коде.

Голос и агент с вызовом функций сюда не входят: они без закрепления до
A/B-замера, и у них нет `schema_name`.
"""

from __future__ import annotations

from sniffer.config import Settings

# schema_name структурного вызова -> роль (суффикс настройки broker_model_<роль>)
ROLE_BY_SCHEMA: dict[str, str] = {
    "query_passport": "intake",
    "search_plan": "planner",
    "offer_screen": "offer_screen",
    "listing_guard": "guard",
    "catalog_facts": "extraction",
}


def pinned_model(settings: Settings, schema_name: str) -> str | None:
    """Модель роли или `None` — пусто и неизвестная схема значат «цепочка брокера»."""
    role = ROLE_BY_SCHEMA.get(schema_name)
    if role is None:
        return None
    value = str(getattr(settings, f"broker_model_{role}", "")).strip()
    return value or None
