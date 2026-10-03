"""Числа суммы: как читаются «9 000 000», «9tr5», «19.5 vnd» и сколько это в донгах.

Только арифметика и запись числа, без контекста: что вокруг суммы и считать ли её
ценой, решает `price_facts.py`.
"""

from __future__ import annotations

import re

from sniffer.domain.price_vocab import UNIT_FACTORS

# 10 млрд донгов — около 380 тысяч долларов. Дороже в Нячанге не продают ни
# байков, ни квартир; потолок закреплён тестом после падения воронки 01.09.2026.
MAX_PLAUSIBLE_VND = 10_000_000_000

# «15 000 ₫» — не цена байка и не аренда квартиры: это либо надбавка, либо
# «15 тысяч», написанное неряшливо. Суточная аренда байка начинается от
# 100 тысяч, так что порог ниже живых цен.
MIN_PLAUSIBLE_VND = 50_000
USD_RANGE = (20, 1_000_000)

# Число длиннее этого — номер, счёт или спам, а не цена. Без предела `float` на
# строке из четырёх тысяч девяток даёт бесконечность, и `int()` роняет воронку
# на одном сообщении (найдено прогоном 4-килобайтных текстов, 03.10.2026).
MAX_DIGITS = 18

# Вьетнамская запись «9tr5» — 9,5 миллиона, «16tr50» — 16,5, «2tr500» — 2,5.
_COMPACT_RE = re.compile(r"(\d+)\s*(tr|triệu|trieu)(\d{1,3})(?!\d)", re.IGNORECASE)
_GROUPED_RE = re.compile(r"\d{1,3}(?:[ .,]\s?\d{3})+")


def expand_compact(text: str) -> str:
    """«9tr5» → «9.5 tr»: так число читается обычным разбором."""

    def spelled(match: re.Match[str]) -> str:
        return f"{match.group(1)}.{match.group(3).rstrip('0') or '0'} {match.group(2)}"

    return _COMPACT_RE.sub(spelled, text)


def factor(unit: str | None) -> int:
    key = (unit or "").casefold()
    return next((value for prefix, value in UNIT_FACTORS if key.startswith(prefix)), 1)


def number(raw: str) -> float:
    """«9 000 000», «9.000.000» — тысячи; «9.5», «9,5» — десятичная дробь."""
    if _GROUPED_RE.fullmatch(raw):
        return float(re.sub(r"[ .,]", "", raw))
    return float(raw.replace(",", "."))


def currency_code(written: str | None) -> str | None:
    """``VND``, ``USD``, ``OTHER`` (рубли, евро: пересчитать нечем) или ``None``."""
    if not written:
        return None
    low = written.casefold()
    if low in {"₫", "đ", "vnđ", "vnd", "внд", "d", "ď"} or low.startswith(("dong", "донг")):
        return "VND"
    return "USD" if low in {"$", "usd"} or low.startswith(("долл", "у")) else "OTHER"


def numbers(match: re.Match[str]) -> tuple[float, float | None, int] | None:
    """Число суммы, верх диапазона и позиция начала суммы в строке; ``None`` — не число цены.

    «№402 — 9 500 000» — не диапазон: первое число там номер, и «до» у него
    втрое больше «от». Живой промах 03.10.2026: десять квартир-строк одного
    объявления читались как «от 402 до 9 500 000» и пропадали.
    """
    if any(len(match.group(name) or "") > MAX_DIGITS for name in ("lo", "hi")):
        return None
    low = number(match.group("lo"))
    if not match.group("hi"):
        return low, None, match.start()
    high = number(match.group("hi"))
    if high > 3 * low:
        return high, None, match.start("hi")
    return low, (high if high > low else None), match.start()


def scale(unit: str | None, written: str | None, code: str | None, value: float) -> int:
    """Множитель суммы: единица, а без единицы у донгов меньше тысячи — миллионы."""
    if unit is None and code == "VND" and value < 1000 and (written or "").casefold() != "d":
        return 10**6  # «19.5 vnd» и «18 vnd» — миллионы: восемнадцати донгов не бывает
    return factor(unit)


def plausible(amount: int, code: str) -> bool:
    if code == "USD":
        return USD_RANGE[0] <= amount <= USD_RANGE[1]
    return MIN_PLAUSIBLE_VND <= amount <= MAX_PLAUSIBLE_VND
