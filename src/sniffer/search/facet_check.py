"""Оставит ли карточку ответ на вопрос сужения — тем же отсевом, что и выдача.

Отчёт по фасетам (`domain.facets`) считает по атрибутам карточки, а отсев
(`relevance._contradicts`) читает текст объявления: лот с `model=vision` в
фактах и без слова «vision» в заголовке отсев не пропускает. Без этой проверки
кнопка «Vision 6» превращалась в пустую выдачу (ревью Opus волны 2, B3). Своего
предиката здесь нет — зовётся тот же `_contradicts`, поэтому разойтись им нечем.
"""

from __future__ import annotations

from collections.abc import Callable

from sniffer.domain.facets import Facetable
from sniffer.domain.fields import apply_answer, parse_option
from sniffer.domain.passport import Passport
from sniffer.search.relevance import contradicts_request
from sniffer.sources.base import RawItem

# Цена отчёта считается по границам корзин, а не по ответу: потолок строится из
# самих цен, и отсев по бюджету карточка уже прошла.
_UNCHECKED = frozenset({"budget.max"})


def answer_survivor(
    passport: Passport, usd_vnd: float | None
) -> Callable[[Facetable, str, str], bool]:
    """Проверка для `facets_from(..., survives=...)`: переживёт ли карточка ответ."""
    answered: dict[tuple[str, str], Passport | None] = {}

    def survives(item: Facetable, field: str, value: str) -> bool:
        if field in _UNCHECKED or not isinstance(item, RawItem):
            return True
        key = (field, value)
        if key not in answered:
            try:
                answered[key] = apply_answer(passport, field, parse_option(field, value))
            except ValueError:
                # Поля без записи реестра (район, год) ответом не заполняются: проверять нечем.
                answered[key] = None
        target = answered[key]
        return True if target is None else not contradicts_request(item, target, usd_vnd)

    return survives
