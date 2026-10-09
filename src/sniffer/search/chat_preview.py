"""Класс кандидата очереди по публичному превью: тема и город - две отдельные проверки.

Тема (`relevant` / `off_topic` / `unknown`) и город (`foreign_city`) выводятся
независимо. Название чата одно ничего не отвергает: `off_topic` требует описания,
`foreign_city` - чужого города именно в описании и отсутствия Нячанга. Слабое
доказательство (слово только в ссылке или контакте) даёт `unknown`.

Словарь рынка здесь не пишется заново: слова категорий и названия городов берутся из
`search.market_terms`; в `domain.chat_preview` лежат только регулярки превью.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from sniffer.domain.chat_preview import (
    FOREIGN_CITY,
    MARKET,
    OFF_TOPIC,
    OFFTOPIC,
    RELEVANT,
    UNKNOWN,
    PreviewSnapshot,
    matched,
    strip_links,
)
from sniffer.search.market_terms import ALL_CITY_NAMES, CATEGORY_TERMS, CITY_ALIASES

HOME_CITY = "nha_trang"
# Написания домашнего города, которых нет в справочнике: слитное и разговорное.
_HOME_EXTRA = ("nhatrang", "нячан")


@dataclass(frozen=True, slots=True)
class PreviewVerdict:
    cls: str
    evidence: str


def _stem(word: str) -> str:
    """Русское название склоняется («в Ханое»): режем последнюю букву, как стем."""
    return word[:-1] if len(word) >= 5 and re.search("[а-яё]", word.lower()) else word


def _alternation(words: list[str]) -> re.Pattern[str]:
    ordered = sorted(set(words), key=len, reverse=True)
    return re.compile("|".join(re.escape(w) for w in ordered), re.IGNORECASE)


@lru_cache(maxsize=1)
def _patterns() -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    category = [w for langs in CATEGORY_TERMS.values() for ws in langs.values() for w in ws]
    home = [*ALL_CITY_NAMES[HOME_CITY].values(), *CITY_ALIASES.get(HOME_CITY, ()), *_HOME_EXTRA]
    foreign = [
        name
        for city, names in ALL_CITY_NAMES.items()
        if city != HOME_CITY
        for name in (*names.values(), *CITY_ALIASES.get(city, ()))
    ]
    stems = [_stem(w) for w in foreign]
    return _alternation(category), _alternation([_stem(w) for w in home]), _alternation(stems)


def _topic(snapshot: PreviewSnapshot) -> tuple[str, str]:
    category, _, _ = _patterns()
    full = f"{snapshot.title}\n{snapshot.description}"
    clean = strip_links(full)
    market = matched(MARKET, clean) + matched(category, clean)
    if market:
        return RELEVANT, f"рыночные слова: {sorted(set(market))[:4]}"
    if matched(MARKET, full) or matched(category, full):
        return UNKNOWN, "слабое доказательство: рыночное слово только в ссылке/контакте"
    if not snapshot.description:
        return UNKNOWN, "нет описания - по одному названию не решаем"
    off = matched(OFFTOPIC, strip_links(snapshot.description))
    if off:
        return OFF_TOPIC, f"описание о другом: {off}; рыночных слов нет"
    if matched(OFFTOPIC, snapshot.description):
        return UNKNOWN, "слабое доказательство: маркер другой темы только в ссылке/контакте"
    return UNKNOWN, "описание есть, но ни рынка, ни явной другой темы"


def _foreign_city(snapshot: PreviewSnapshot) -> str | None:
    """Чужой город - только из описания и только если Нячанга нет нигде в превью."""
    _, home, foreign = _patterns()
    if not snapshot.description:
        return None
    if home.search(strip_links(f"{snapshot.title}\n{snapshot.description}")):
        return None
    cities = matched(foreign, strip_links(snapshot.description))
    return f"чужой город в описании: {cities}; Нячанга нет" if cities else None


def classify_preview(snapshot: PreviewSnapshot) -> PreviewVerdict:
    if snapshot.status != "ok":
        why = f"превью недоступно: {snapshot.status} {snapshot.extra}".strip()
        return PreviewVerdict(UNKNOWN, why)
    topic, why = _topic(snapshot)
    city = _foreign_city(snapshot)
    if city:
        return PreviewVerdict(FOREIGN_CITY, f"{city}; тема={topic}")
    return PreviewVerdict(topic, why)
