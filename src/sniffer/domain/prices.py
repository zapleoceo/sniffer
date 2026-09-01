"""Консервативное чтение цены из свободного текста.

Это не источник и не extractor: одно и то же знание нужно живому Telegram
поиску и обработке накопленного архива. Число признаём ценой только после
явной метки, иначе год, пробег и ``125cc`` становятся ложными донгами.
"""

from __future__ import annotations

import re

_PRICE_RE = re.compile(
    r"(?:цена|price|giá|gia)\s*[:—-]?\s*"
    r"(?P<number>\d+(?:[ .]\d{3})+|\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>млн\.?|мил\.?|million|triệu|tr|m|тыс\.?|k)?\s*"
    r"(?P<currency>₫|đ|vnd|dong|донг(?:ов)?)?",
    re.IGNORECASE,
)


def price_hint(text: str) -> tuple[str, int | None]:
    """Вернуть написанную цену и её значение в VND либо честное ``None``."""
    match = _PRICE_RE.search(text)
    if match is None:
        return "", None
    raw_number = match.group("number")
    number = (
        raw_number.replace(" ", "").replace(".", "")
        if re.fullmatch(r"\d{1,3}(?:[ .]\d{3})+", raw_number)
        else raw_number.replace(",", ".")
    )
    try:
        value = float(number)
    except ValueError:  # pragma: no cover -- регулярное выражение оставляет число
        return "", None
    unit = (match.group("unit") or "").casefold().rstrip(".")
    currency = (match.group("currency") or "").casefold()
    multiplier = 1_000_000 if unit in {"млн", "мил", "million", "triệu", "tr", "m"} else 1
    if unit in {"тыс", "k"}:
        multiplier = 1_000
    if multiplier == 1 and not currency:
        return "", None
    price = int(value * multiplier)
    return match.group(0).strip(), price if price > 0 else None
