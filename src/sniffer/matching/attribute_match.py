"""Совпадает ли атрибут карточки с атрибутом паспорта: числа сравниваются по смыслу.

Раньше сравнивали строки: `"65" != "65.0"`, а «залог 1,5 месяца» против «залог 1» — всегда
конфликт. Из-за этого `worth_sending` отсекал подходящее: клиент просит «60 метров», в
карточке 64.5, и автоуведомление молчит. Знание о допуске по каждому числовому полю —
одно, здесь; и отсечение (`conflicts`), и доля совпавших (`matches`) спрашивают его же.

Допуски — решение, а не измерение: клиент называет «примерно», а карточка пишет точное.
- площадь: ±15% (60 м2 и 64 м2 — одна квартира по запросу);
- месяцы залога и срока: ±0,5 («1» и «1,5» — разница в слове, «1» и «2» — в деньгах);
- объём двигателя: полоса `ENGINE_CC_BAND` и направление «от/до» (`engine_cc_dir`),
  как у отбора выдачи (`passport.engine_cc_bounds`);
- всё прочее число (комнаты, этаж, год, пробег) — точное совпадение, но «2» и «2.0»
  равны, а «2» и «3» нет.
Нечисловое (марка, коробка, булево) сравнивается как раньше: строка без учёта регистра.
"""

from __future__ import annotations

from collections.abc import Mapping

from sniffer.domain.passport import engine_cc_bounds

AREA_RELATIVE_TOLERANCE = 0.15
MONTHS_TOLERANCE = 0.5
_MONTH_FIELDS = frozenset({"deposit_months", "min_term_months"})


def as_number(value: object) -> float | None:
    """Число из значения атрибута; булево и нечисловое — `None` (булево не количество)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", "."))
        except ValueError:
            return None
    return None


def matches(field: str, actual: object, wanted: object, wanted_all: Mapping[str, object]) -> bool:
    """Карточка со значением `actual` отвечает требованию паспорта `wanted`."""
    have, want = as_number(actual), as_number(wanted)
    if have is None or want is None:
        return str(actual).casefold() == str(wanted).casefold()
    if field == "engine_cc":
        low, high = engine_cc_bounds(int(want), wanted_all.get("engine_cc_dir"))
        return (low is None or have >= low) and (high is None or have <= high)
    if field == "area_m2":
        return abs(have - want) <= want * AREA_RELATIVE_TOLERANCE
    if field in _MONTH_FIELDS:
        return abs(have - want) <= MONTHS_TOLERANCE
    return have == want


def conflicts(field: str, actual: object, wanted: object, wanted_all: Mapping[str, object]) -> bool:
    """Карточка называет значение, и оно противоречит требованию; неназванное — не конфликт."""
    if actual in (None, ""):
        return False
    return not matches(field, actual, wanted, wanted_all)
