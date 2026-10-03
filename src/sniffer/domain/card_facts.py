"""Факты карточки из атрибутов: «Honda Lead · 110 cc · 2008 · автомат».

Одна функция на все поверхности — выдачу в чате, мониторинг и отложенные ответы.
Раньше карточка показывала только заголовок, цену и дату, хотя марка, объём, год
и пробег лежат в `listings.attributes` и по ним клиент и выбирает (R4 §1.3): три
места, пересказывающие одно и то же знание своими словами, разошлись бы при первой
правке. Здесь только текст без разметки: экранирование — дело того, кто вставляет
строку в HTML.

Чего нет в атрибутах — того нет и в строке. «Не нашёл» не печатаем: пропуск
в тексте объявления не значит, что у лота этого факта нет.
"""

from __future__ import annotations

from collections.abc import Mapping

_TRANSMISSION = {"automatic": "автомат", "manual": "механика", "semi": "полуавтомат"}
# Потолок строки: атрибуты приходят из разбора чужого текста, и один кривой
# `model` на пол-абзаца не должен раздувать карточку.
MAX_FACTS_LEN = 120
_PART_LEN = 40


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return value


def _name(attributes: Mapping[str, object]) -> str:
    words = [
        str(attributes[key]).replace("_", " ").strip().title()
        for key in ("brand", "model")
        if isinstance(attributes.get(key), str) and str(attributes[key]).strip()
    ]
    return " ".join(words)[:_PART_LEN]


def fact_parts(attributes: Mapping[str, object]) -> list[str]:
    """Факты по порядку «что → объём → год → коробка → пробег → площадь»."""
    parts = [_name(attributes)]
    if (cc := _number(attributes.get("engine_cc"))) is not None:
        parts.append(f"{cc:g} cc")
    if (year := _number(attributes.get("year"))) is not None:
        parts.append(f"{year:g}")
    gearbox = attributes.get("transmission")
    if isinstance(gearbox, str):
        parts.append(_TRANSMISSION.get(gearbox, ""))
    if (km := _number(attributes.get("mileage_km"))) is not None:
        parts.append(f"{km:,.0f} км".replace(",", "\u00a0"))
    if (area := _number(attributes.get("area_m2"))) is not None:
        parts.append(f"{area:g} м²")
    return [part for part in parts if part]


def _squash(text: str) -> str:
    return "".join(text.casefold().split())


def facts_line(attributes: Mapping[str, object] | None, *, title: str = "") -> str:
    """Строка фактов без повторов заголовка: «Honda Lead» в заголовке не пишем снова."""
    if not attributes:
        return ""
    seen = _squash(title)
    parts = [part for part in fact_parts(attributes) if _squash(part) not in seen]
    line = " · ".join(parts)
    return line if len(line) <= MAX_FACTS_LEN else line[: MAX_FACTS_LEN - 1].rstrip() + "…"
