"""Расстояние до моря из текста: минуты пешком или метры, как написано.

Минуты и метры не пересчитываются друг в друга: «5 минут» — это шаг пешком, а «≈ 10
минут на байке до моря» (так пишет агентство дальних домов) — другое расстояние, и его
не читаем вовсе. Пересчёт по скорости ходьбы выдавал бы выдумку за факт, а клиент,
которому нужно «до моря пешком», получил бы дом в десяти минутах езды.
"""

from __future__ import annotations

import re

from sniffer.domain.facts_text import FactText

MAX_MINUTES, MAX_METERS = 90, 20_000
_SEA = r"(?:мор[яеюй]\b|пляж\w*|океан\w*|beach|sea\b|ocean|bien\b)"
_TO = r"\b(?:до|от|to|from|away from|toi|den|ra|cach)\b"
_MIN = r"(?:мин\w*|min\w*|phut)"
_LEN = r"(?:метр\w*|м(?![\w])|meters?|metres?|m(?![\w])|km|км)"
_TRANSPORT = re.compile(
    r"на\s+(?:байк|машин|авто|скутер)|by\s+(?:bike|motorbike|scooter|car|taxi)|driv"
)
_NUM = r"(?<![\d.,])(?P<num>\d{1,5})(?![\d.,]\d)"
# Два порядка слов: «5 минут до моря», «в 300 метрах от пляжа» — и «до пляжа 100 метров»,
# «до моря минут 7». Между числом и морем — не больше 24 знаков одной строки.
_NUMBER_FIRST = re.compile(
    rf"{_NUM}[ \t-]{{0,2}}(?P<unit>{_MIN}|{_LEN})(?P<mid>[^\n.]{{0,24}}?)"
    rf"{_TO}\s{{0,2}}(?:the\s+)?{_SEA}"
)
_SEA_FIRST = re.compile(
    rf"{_TO}\s{{0,2}}{_SEA}\s{{0,2}}[-–—:~≈]?\s{{0,2}}(?:около\s+|about\s+)?"
    rf"(?P<pre>{_MIN})?\s{{0,2}}{_NUM}\s{{0,2}}(?P<unit>{_MIN}|{_LEN})?(?P<mid>)"
)


def read_sea_distance(text: FactText) -> dict[str, int]:
    """`sea_distance_min` или `sea_distance_m` — первое названное; пусто, если не названо."""
    for line in text.folded.splitlines():
        for pattern in (_NUMBER_FIRST, _SEA_FIRST):
            for match in pattern.finditer(line):
                tail = line[match.end() : match.end() + 14]
                if _TRANSPORT.search(f"{match.group('mid')} {tail}"):
                    continue
                unit = match.group("unit") or match.groupdict().get("pre") or ""
                if not unit:
                    continue  # «до моря 50» — чего именно, не сказано
                value = int(match.group("num"))
                if re.match(_MIN, unit):
                    return {"sea_distance_min": value} if 0 < value <= MAX_MINUTES else {}
                meters = value * 1000 if unit.startswith(("km", "км")) else value
                return {"sea_distance_m": meters} if 0 < meters <= MAX_METERS else {}
    return {}
