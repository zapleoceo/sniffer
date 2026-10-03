"""Этаж квартиры и этажность дома из текста.

Два разных числа под одним словом: «5 этаж» у квартиры — на каком она этаже, а
«3-этажный дом» или «4 tầng» у дома — сколько в нём этажей. Перепутать их значит
сказать клиенту, что дом стоит на третьем этаже. Поэтому читаются они по-разному,
а у квартиры в расчёт идёт только порядковое число («5 этаж», «этаж: 5», «5/12 эт.»,
«tầng 5», «5th floor»), но не количество («25 этажей в здании»).

Чужие этажи не считаются: «бассейн на 31 этаже», «общая стиральная машина на 8
этаже», «sân thượng trên tầng 4» — это этаж удобства, а не лота. А названы разные
этажи («1 этаж … 3 этаж») — это каталог лотов или описание дома по этажам, и этажа у
лота нет: честнее промолчать, чем выбрать первый.

Вьетнамское «lầu N» — на единицу выше «N»: этажом 1 там зовётся тот, что над «tầng
trệt» (первым), и «lầu 3 / 4th floor» в одном тексте — один и тот же этаж.
"""

from __future__ import annotations

import re

from sniffer.domain.facts_text import FactText

MAX_FLOOR = 60
_N = r"(\d{1,2})"
_ORDINAL_END = r"(?:й|ый|ой|ий|го|м|ом|е)"
_WORD_ORDINALS = {
    "перв": 1, "втор": 2, "трет": 3, "четверт": 4, "пят": 5,
    "шест": 6, "седьм": 7, "восьм": 8, "девят": 9, "десят": 10,
}  # fmt: skip
_RANGE_TAIL = r"(?:\s{0,2}[-–—]\s{0,2}\d{1,2})?"
# Каждое из этих выражений называет, НА КАКОМ этаже квартира. «1–2 этаж» — меньший.
_FLOOR = (
    re.compile(rf"(?<![\d./]){_N}{_RANGE_TAIL}[ \t]{{0,2}}этаж(?:е)?\b"),
    re.compile(rf"(?<![\d./]){_N}[ \t]{{0,2}}-[ \t]{{0,2}}{_ORDINAL_END}[ \t]{{0,2}}этаж\w*"),
    re.compile(rf"\bэтаж(?:е)?\b[ \t]{{0,2}}[:=\-–—]?[ \t]{{0,2}}{_N}(?!\d)"),
    re.compile(rf"(?<![\d.]){_N}[ \t]{{0,2}}/[ \t]{{0,2}}\d{{1,2}}[ \t]{{0,2}}(?:этаж\w*|эт\b)"),
    re.compile(rf"(?<!\d){_N}(?:st|nd|rd|th)?[\s-]{{0,2}}floor\b"),
    re.compile(rf"\bfloor[ \t]{{0,2}}[:=\-]?[ \t]{{0,2}}{_N}(?!\d)"),
)
_WORD_ORDINAL = re.compile(rf"\b({'|'.join(_WORD_ORDINALS)})\w{{0,3}}[ \t]{{0,2}}этаж(?:е)?\b")
# Вьетнамское «tầng» читается по тексту С диакритикой: без неё «tang» — ещё и «tặng»
# («подарок»), и «tặng 1 tháng» стало бы первым этажом.
_VI_FLOOR = (
    re.compile(rf"tầng[ \t]{{0,2}}(?:thứ[ \t]{{0,2}})?{_N}(?!\d)"),
    re.compile(rf"\btang[ \t]{{0,2}}{_N}(?!\d)"),
)
_VI_LAU = re.compile(rf"\blầu[ \t]{{0,2}}{_N}(?!\d)")
_GROUND = re.compile(r"ground floor|tầng trệt|lầu trệt|\btang tret\b")
# Этаж не лота, а того, что при нём: бассейна, прачечной, террасы, кровли.
_FACILITY = re.compile(
    r"бассейн|спортзал|тренажер|стиральн|прачечн|\bсад|террас|парковк|ресторан|кафе|\bбар\b|"
    r"игров|детск|крыш|pool|gym|laundry|garden|terrace|parking|restaurant|playroom|lobby|"
    r"reception|roof|hồ bơi|phòng gym|sân thượng|nhà xe|nhà hàng"
)
# Сколько этажей в доме, а не на каком стоит лот.
_STOREYS = (
    re.compile(rf"{_N}[ \t]{{0,2}}-?[ \t]{{0,2}}этажн\w*"),
    re.compile(rf"(?<!\d){_N}[ \t]{{0,2}}этаж(?:а|ей)\b"),
    re.compile(rf"\bэтажность[ \t]{{0,2}}[:=\-–—]?[ \t]{{0,2}}{_N}(?!\d)"),
    re.compile(rf"(?<!\d){_N}[\s-]{{0,2}}(?:floors|stor(?:e)?y|stories|storeys)\b"),
)
_VI_STOREYS = re.compile(rf"(?<!\d){_N}[ \t]{{0,2}}tầng\b")


def _values(patterns: tuple[re.Pattern[str], ...], text: str, *, shift: int = 0) -> list[int]:
    """Числа, найденные шаблонами, кроме тех, что стоят после слова-удобства в той же строке."""
    found: list[int] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            before = text[max(0, match.start() - 40) : match.start()]
            if not _FACILITY.search(before[before.rfind("\n") + 1 :]):
                found.append(int(match.group(1)) + shift)
    return found


def _distinct(values: list[int]) -> int | None:
    unique = {value for value in values if 0 < value <= MAX_FLOOR}
    return unique.pop() if len(unique) == 1 else None


def read_floor(text: FactText) -> int | None:
    """Этаж квартиры или комнаты; `None` — не назван или названы разные."""
    found = _values(_FLOOR, text.folded) + _values(_VI_FLOOR, text.low)
    found += _values((_VI_LAU,), text.low, shift=1)
    found += [
        _WORD_ORDINALS[match.group(1)]
        for match in _WORD_ORDINAL.finditer(text.folded)
        if not _FACILITY.search(
            text.folded[max(0, match.start() - 40) : match.start()].rpartition("\n")[2]
        )
    ]
    found += [1 for _ in _GROUND.finditer(text.low)]
    return _distinct(found)


def read_storeys(text: FactText) -> int | None:
    """Сколько этажей в доме; `None` — не названо или названо по-разному."""
    found = _values(_STOREYS, text.folded) + _values((_VI_STOREYS,), text.low)
    return _distinct(found)
