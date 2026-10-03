"""Район и зона города из текста объявления: где лот, а не что рядом с ним.

Место ищется в трёх кругах по убыванию доверия, и первый круг, где оно названо, решает:

1. строки-метки — «Локация:», «Район:», «Location:», «📍 …» и список под меткой: так
   агентство отвечает на вопрос «где» прямо;
2. первые три строки поста — там стоит название лота («Квартира в Oceanus»);
3. весь текст без строк-меню (`facts_text`): место из подвала («АРЕНДА В ЖК ОКЕАНУС» —
   меню) район лота не называет, а это был самый частый ложный район.

Внутри круга вард и район главнее ЖК, ЖК главнее улицы: «Oceanus, Phước Hải» — это район
Phước Hải. Названа улица («Trần Phú», «Hùng Vương») — она есть почти в каждом городе, и
при споре городов теряет силу первой: «Trần Phú, Hải Châu, Da Nang» — дананский адрес.

Зона — слова самого поста («Север Нячанга»), а если их нет — зона названного места из
справочника, где она проставлена. Слово зоны, перед которым стоит «до/от/рядом с», —
расстояние до центра, а не положение лота («10 минут до центра города»).

Город названного места не равен городу карточки, и это нарочно: так видно, что нячангская
карточка называет Hải Châu. Менять `city` по нему здесь никто не вправе — это решение
владельца (docs/architecture.md, 5.0.3).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from sniffer.domain.districts import (
    KIND_RANK,
    NOT_CENTER,
    PLACES,
    REFERENCE_BEFORE,
    ZONE_PHRASES,
    ZONE_WORDS,
    Place,
)
from sniffer.domain.facts_text import FactText
from sniffer.domain.facts_vocab import NEARBY_RE

HEAD_LINES = 3
LABEL_FOLLOWERS = 4
_SEPARATORS = re.compile(r"[\s\-_.,;:()\[\]|/•·]+")
_CYRILLIC = re.compile(r"[а-я]")
_LABEL = re.compile(
    r"^[^\w\n]*(?:локаци\w*|район\w*|расположени\w*|адрес|location|area|address|adress|district|"
    r"dia chi|khu vuc)[^\w\n]{0,3}(?P<value>.*)$"
)
_PIN = re.compile(r"^[^\w\n]{0,4}📍[^\w\n]*(?P<value>.*)$")
_BULLET = re.compile(r"^\s*[•\-–—➖▪·*]")
_DANANG = re.compile(r"\bda ?nang\b|дананг")
_SEGMENTS = re.compile(r"[,;|•·—–()\[\]/]|\s-\s")


def _norm(text: str) -> str:
    return _SEPARATORS.sub(" ", text).strip()


_BY_ALIAS = {_norm(alias): place for place in PLACES for alias in place.aliases}


def _alias_pattern(alias: str) -> str:
    """Русское название склоняется («в Фуок Хае», «Лок Тхо»): у кириллического написания
    допускаем два лишних конечных знака, а «-й» — ещё и заменой на «-е/-я/-ю». Латинские
    написания — только целиком."""
    if not _CYRILLIC.search(alias):
        return re.escape(alias)
    if alias.endswith("й"):
        return re.escape(alias[:-1]) + r"(?:й|е|я|ю)[а-я]{0,2}"
    return re.escape(alias) + r"[а-я]{0,2}"


_PLACE_RE = re.compile(
    r"(?<![a-zа-я0-9])(?:"
    + "|".join(_alias_pattern(alias) for alias in sorted(_BY_ALIAS, key=len, reverse=True))
    + r")(?![a-zа-я0-9])"
)
_ZONE_RE = {zone: re.compile(pattern) for zone, pattern in ZONE_PHRASES.items()}
_ZONE_WORD_RE = {zone: re.compile(pattern) for zone, pattern in ZONE_WORDS.items()}
_NOT_CENTER_RE = re.compile(NOT_CENTER)
_REFERENCE_RE = re.compile(REFERENCE_BEFORE)


@dataclass(frozen=True, slots=True)
class PlaceFact:
    """Что пост говорит о месте: слаг района, зона и город названного места."""

    district: str | None = None
    zone: str | None = None
    city: str | None = None


def _place_of(found: str) -> Place:
    for cut in range(len(found), max(len(found) - 3, 0), -1):
        head = found[:cut]
        for written in (head, head[:-1] + "й"):
            if (place := _BY_ALIAS.get(written)) is not None:
                return place
    raise KeyError(found)  # pragma: no cover -- шаблон собран из тех же написаний


def _places(region: str) -> list[Place]:
    return [_place_of(match.group(0)) for match in _PLACE_RE.finditer(_norm(region))]


def _labeled(lines: list[str]) -> list[str]:
    """Значения строк-меток; у пустой метки («📍 Локация:») — маркированный список под ней."""
    values: list[str] = []
    for index, line in enumerate(lines):
        match = _LABEL.match(line) or _PIN.match(line)
        if match is None:
            continue
        if value := match.group("value").strip():
            values.append(value)
            continue
        for follower in lines[index + 1 : index + 1 + LABEL_FOLLOWERS]:
            if not _BULLET.match(follower):
                break
            values.append(follower)
    return values


def _zone(region: str, *, standalone: bool) -> str | None:
    """Зона, названная в круге; `None` — не названа или названы разные."""
    cleaned = NEARBY_RE.sub(" ", _NOT_CENTER_RE.sub(" ", region))
    zones: set[str] = set()
    for zone, pattern in _ZONE_RE.items():
        for match in pattern.finditer(cleaned):
            before = cleaned[max(0, match.start() - 18) : match.start()]
            if not _REFERENCE_RE.search(before[before.rfind("\n") + 1 :]):
                zones.add(zone)
                break
    if standalone:
        for segment in _SEGMENTS.split(cleaned):
            word = re.sub(r"[^\w\s-]", "", segment).strip()
            zones.update(zone for zone, pattern in _ZONE_WORD_RE.items() if pattern.fullmatch(word))
    return zones.pop() if len(zones) == 1 else None


def _city(found: list[Place], body: str) -> str | None:
    """Город, к которому относится пост: у кого больше доказательств, а не улиц."""
    # Голосует место, а не упоминание: «Mường Thanh» трижды — это одно доказательство.
    votes = Counter(place.city for place in set(found) if place.kind != "street")
    if _DANANG.search(body):
        votes["da_nang"] += 2
    if not votes:
        return found[0].city if found else None
    leaders = {city for city, count in votes.items() if count == max(votes.values())}
    # При равенстве побеждает тот город, чьё место названо раньше.
    return next((place.city for place in found if place.city in leaders), next(iter(leaders)))


def _district(found: list[list[Place]], city: str | None) -> tuple[Place | None, int]:
    """Лучшее место города и номер круга, где оно названо."""
    for index, round_ in enumerate(found):
        if candidates := [place for place in round_ if place.city == city]:
            return min(candidates, key=lambda place: KIND_RANK[place.kind]), index
    return None, -1


def read_place(text: FactText) -> PlaceFact:
    """Район, зона и город места, названного в посте; пустые поля — не названо."""
    lines = text.folded.splitlines()
    regions = ("\n".join(_labeled(lines)), "\n".join(lines[:HEAD_LINES]), text.folded)
    city = _city(_places(regions[2]), text.folded)
    district, circle = _district([_places(region) for region in regions], city)
    zones = [_zone(region, standalone=index < 2) for index, region in enumerate(regions)]
    # Место, названное лишь в подвале и спорящее с зоной из метки или заголовка, — меню
    # агентства («АРЕНДА В ЖК ОКЕАНУС» в посте про центр), а не адрес лота.
    if (
        district
        and circle == 2
        and district.zone
        and (zones[0] or zones[1]) not in (None, district.zone)
    ):
        district = None
    zone = None
    if city != "da_nang":
        zone = next((z for z in zones if z), district.zone if district else None)
    return PlaceFact(district.slug if district else None, zone, city)
