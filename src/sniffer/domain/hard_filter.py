"""Жёсткий фильтр подписки: условия, которых нельзя не соблюдать, и место, которое нельзя расширять.

Обычный отбор слежения («известное ≠ несовпадение», `matching.worth_sending`) мягкий: карточка,
о балконе которой ничего не известно, проходит, потому что половина объявлений его не пишет.
Для владельца, который сказал «без отдельной кухни и балкона не присылать», это неверно: неизвестное
тут равно «нет». Поэтому условия живут отдельно, лежат на ПОДПИСКЕ (`subscriptions.hard_filter`,
а не в паспорте: правка паспорта их не стирает, а другие клиенты их не видят) и по умолчанию
отсутствуют — подписка без фильтра ведёт себя как прежде байт в байт.

Что проверяется и откуда берётся знание:

* `balcony` — атрибут `balcony` карточки; нет атрибута — слова текста (ru / en / vi) без отрицания;
* `separate_kitchen` — атрибут `kitchen == "separate"`; `shared` — отказ; нет атрибута — слова
  текста о ОТДЕЛЬНОЙ кухне. Просто «кухня» не годится: она есть в каждой квартире, включая студии;
* место — `listings.district` из справочника `districts_nha_trang` (слаги) либо, когда справочник
  района не нашёл, слова-адреса из `place_words` (улицы, которых в справочнике нет). Известный район
  ВНЕ списка — отказ: «Gò Găng» и юг не подходят, и расширять список здесь нечем. Координат у
  карточек нет, радиус от точки считать не из чего, и выдумывать его по названию улицы нельзя.

Разбор JSON падает на неизвестном ключе или требовании: опечатка в фильтре, молча превратившаяся в
«фильтра нет», разослала бы всё подряд — подписка с больным фильтром уходит в карантин.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sniffer.domain.districts import PLACE_BY_SLUG
from sniffer.domain.facts_text import fold

if TYPE_CHECKING:
    from sniffer.domain.records import Listing

BALCONY = "balcony"
SEPARATE_KITCHEN = "separate_kitchen"
REQUIREMENTS = frozenset({BALCONY, SEPARATE_KITCHEN})
_KEYS = frozenset({"require", "districts", "place_words"})

# Слова на свёрнутом тексте (`fold`: латиница без диакритики, «đ» → «d», кириллица нетронута).
_BALCONY = re.compile(r"балкон|\bbalcon(?:y|ies)?\b|\bban cong\b")
_SEPARATE_KITCHEN = re.compile(
    r"отдельн\w*\s+(?:\w+\s+)?кухн|раздельн\w*\s+кухн|кухн\w*\s+отдельн|"
    r"separate\s+(?:\w+\s+)?kitchen|kitchen\s+(?:is\s+)?separate|"
    r"\bbep\s+rieng\b|\bphong\s+bep\s+rieng\b|\bbep\s+tach\b"
)
# Отрицание прямо перед словом условия: «без балкона», «no balcony», «không có ban công».
_NEGATION = re.compile(r"(?:^|\s)(?:без|нет|не|no|not|without|khong co|khong)\s+(?:\w+\s+){0,2}$")
_WINDOW = 24


def _mentioned(pattern: re.Pattern[str], folded: str) -> bool:
    """Слово условия есть в тексте и не под отрицанием."""
    for match in pattern.finditer(folded):
        if not _NEGATION.search(folded[max(0, match.start() - _WINDOW) : match.start()]):
            return True
    return False


@dataclass(frozen=True, slots=True)
class HardFilter:
    """Условия подписки. Пустой фильтр ничего не требует и ничего не отсеивает."""

    require: frozenset[str] = frozenset()
    # Слаги районов справочника, в которых карточка допустима. Пусто — место не ограничено.
    districts: frozenset[str] = frozenset()
    # Слова-адреса (свёрнутые), которых нет в справочнике: улицы у ориентира.
    place_words: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, raw: dict[str, Any] | None) -> HardFilter | None:
        """`None` в базе — фильтра нет. Незнакомое в фильтре — ошибка, а не пропуск."""
        if raw is None:
            return None
        unknown = set(raw) - _KEYS
        if unknown:
            raise ValueError(f"hard_filter: неизвестные ключи {sorted(unknown)}")
        require = frozenset(raw.get("require") or ())
        if not require <= REQUIREMENTS:
            raise ValueError(
                f"hard_filter: неизвестные требования {sorted(require - REQUIREMENTS)}"
            )
        districts = frozenset(raw.get("districts") or ())
        missing = sorted(slug for slug in districts if slug not in PLACE_BY_SLUG)
        if missing:
            raise ValueError(f"hard_filter: районов нет в справочнике {missing}")
        words = tuple(fold(str(word)).strip() for word in raw.get("place_words") or ())
        if any(not word for word in words):
            raise ValueError("hard_filter: пустое слово-адрес")
        return cls(require, districts, words)

    def to_json(self) -> dict[str, Any]:
        return {
            "require": sorted(self.require),
            "districts": sorted(self.districts),
            "place_words": list(self.place_words),
        }

    def accepts(self, listing: Listing) -> bool:
        """Карточка удовлетворяет ВСЕМ условиям. Неизвестное отсеивается, не пропускается."""
        folded = fold(f"{listing.title}\n{listing.summary}")
        return (
            self._place_ok(listing.district, folded)
            and (BALCONY not in self.require or _balcony(listing.attributes, folded))
            and (
                SEPARATE_KITCHEN not in self.require
                or _separate_kitchen(listing.attributes, folded)
            )
        )

    def _place_ok(self, district: str | None, folded: str) -> bool:
        if not self.districts and not self.place_words:
            return True
        if district in self.districts:
            return True
        # Адрес-ориентир назван в тексте прямо: перекрывает район, который справочник
        # выбрал по соседнему упоминанию.
        return any(
            re.search(rf"(?<!\w){re.escape(word)}(?!\w)", folded) for word in self.place_words
        )


def _balcony(attributes: dict[str, Any], folded: str) -> bool:
    known = attributes.get("balcony")
    if isinstance(known, bool):
        return known
    return _mentioned(_BALCONY, folded)


def _separate_kitchen(attributes: dict[str, Any], folded: str) -> bool:
    known = attributes.get("kitchen")
    if known == "separate":
        return True
    if known is not None:
        return False
    return _mentioned(_SEPARATE_KITCHEN, folded)
