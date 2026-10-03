"""Площадь жилья из текста.

Число названо в половине постов и легко читается неверно: у объявления дома несколько
площадей (участок, полезная), у квартиры — площади комнат и балкона. Поэтому у каждого
найденного числа смотрится, что стоит перед ним, и берётся не первое, а подходящее:

- «Полезная/общая площадь» главнее просто «Площади», а та главнее числа с одной
  лишь единицей («студия 35 м²»);
- участок, сад, двор и площади частей жилья (спальня, кухня, балкон) — не площадь лота;
- «35–40 м²» — несколько лотов в одном посте; берётся меньшая граница.
"""

from __future__ import annotations

import re

from sniffer.domain.facts_text import FactText

MIN_AREA_M2, MAX_AREA_M2 = 8, 1000

_NUM = r"\d{1,4}(?:[.,]\d{1,2})?"
_SQUARE = r"(?:м2|кв\.?\s?м\.?|квм|m2|sqm|sq\.?\s?m\b|square\s+met(?:er|re)s?)(?![a-zа-я0-9])"
_RANGE = rf"(?P<lo>{_NUM})(?:\s{{0,2}}[-–—]\s{{0,2}}(?P<hi>{_NUM}))?"
# Число не должно быть хвостом «1 200», долей и частью даты или номера.
_START = r"(?<![\d.,/])(?<!\d\s)"
_WITH_UNIT = re.compile(rf"{_START}{_RANGE}\s{{0,2}}{_SQUARE}")
# После метки единица необязательна: «Площадь: 35», «Diện tích : 40m». Но число без единицы
# обязано кончаться: «Площадь: 40 млн/месяц» (так агентство пишет цену) и «Площадь 8x15м»
# (размеры участка) площадью лота не являются.
_LABEL = r"(?:площад\w*|area|dien tich\w*)"
_LABELED = re.compile(
    rf"{_LABEL}\s{{0,2}}(?:[:=\-–—]|is)?\s{{0,2}}(?:около|~|≈|about)?\s{{0,2}}{_RANGE}"
    r"(?=\s{0,2}(?:м\b|m\b|кв|$|[,.;|(]))"
)
# Слово, стоящее ВПЛОТНУЮ к числу и называющее участок или часть жилья: «участок: 200 м²»,
# «спальня 15 м²». «3 спальни, 180 м²» — запятая отделяет: это площадь всего лота. И
# «1 bedroom (50 м²)» — не площадь спальни: число перед словом делает его составом лота.
_ADJACENT = r"\w*\s{0,2}[:=\-–—(]{0,2}\s{0,2}$"
_LAND = re.compile(rf"(?:участк|земл|plot|\bland|\blot|\bdat|\bсад|garden|двор|yard){_ADJACENT}")
_PART = re.compile(
    r"(?<!\d)(?<!\d\s)(?:спальн|bedroom|кухн|kitchen|гостин|living|ванн|bath|балкон|balcon|"
    rf"террас|terrace|гараж|парковк){_ADJACENT}"
)
_USABLE = re.compile(r"полезн|жил|общ|su dung|usable|total|gross|floor area|living area")
_LABEL_BEFORE = re.compile(rf"{_LABEL}\W{{0,3}}$")


def _number(raw: str) -> float:
    value = float(raw.replace(",", "."))
    return int(value) if value == int(value) else value


def _rank(line: str, start: int) -> int | None:
    """Чем подтверждено число: 0 — «полезная/общая», 1 — метка вплотную, 2 — только единица."""
    before = line[max(0, start - 40) : start]
    if _LAND.search(before) or _PART.search(before):
        return None
    if _USABLE.search(before):
        return 0
    return 1 if _LABEL_BEFORE.search(before) else 2


def read_area(text: FactText) -> float | None:
    """Площадь лота в м²: лучше подтверждённая, а при равенстве — первая названная."""
    best: tuple[tuple[int, int, int], float] | None = None
    for index, line in enumerate(text.folded.splitlines()):
        for pattern, labeled in ((_WITH_UNIT, False), (_LABELED, True)):
            for match in pattern.finditer(line):
                rank = _rank(line, match.start("lo"))
                value = _number(match.group("lo"))
                if rank is None or (labeled and rank == 2):
                    continue
                if not MIN_AREA_M2 <= value <= MAX_AREA_M2:
                    continue
                key = (rank, index, match.start())
                if best is None or key < best[0]:
                    best = (key, value)
    return best[1] if best else None
