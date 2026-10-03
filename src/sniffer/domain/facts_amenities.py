"""Удобства жилья из текста: есть, нет или не сказано.

Три исхода, а не два: «без балкона» — это `False`, а молчание — `None`, и путать их
нельзя. Клиент, которому нужен лифт, не должен терять лот, где про лифт не написано
(половина объявлений удобства не называет), но обязан не получить лот, где написано
«без лифта». Поэтому отрицание ищется отдельно и побеждает молчание.

Что считается отрицанием и что нет — из живых текстов (03.10.2026):

- «без балкона», «no elevator», «không có thang máy» — вплотную к предмету; список
  «без балкона, лифта и кондиционера» отрицает все три, а «без комиссии, лифт» —
  нет: слово после «без» не удобство, и список на нём не начинается;
- «лифт: нет», «балкона нет, но есть окна» — после предмета, но оборот обязан
  кончиться: в «с лифтами нет проблем» «нет» говорит о проблемах;
- «без использования лифта», «без учёта балкона» — не отрицание: лифт и балкон
  там как раз есть, и вплотную к слову «без» они не стоят;
- «Рядом: бассейн» — соседство, а не удобство лота (`NEARBY_RE`);
- «с балконом и без балкона» — противоречие: каталог из нескольких лотов, а не ответ.
"""

from __future__ import annotations

import re

from sniffer.domain.facts_text import FactText
from sniffer.domain.facts_vocab import (
    ABSENT_AFTER,
    AMENITIES,
    CLAUSE_SPLIT,
    KITCHEN_SEPARATE,
    KITCHEN_SHARED,
    NEARBY_RE,
    NEGATOR,
    PARTIAL_BEFORE_RE,
    PETS_NO,
    PETS_STEM,
    PETS_YES,
    PRICED_CLAUSE,
)

_ANY = "|".join(AMENITIES.values())
_STEM = {key: re.compile(stem) for key, stem in AMENITIES.items()}
_ABSENT = {
    key: re.compile(rf"(?:{stem})[ \t]{{0,2}}[:\-–—]?[ \t]{{0,2}}{ABSENT_AFTER}", re.MULTILINE)
    for key, stem in AMENITIES.items()
}
# «Без использования лифта» — там лифт как раз есть, а сказано лишь, что ходить к нему не надо.
_NEUTRAL = {
    key: re.compile(
        rf"без[ \t]+(?:\w+[ \t]+(?:и|или)[ \t]+)?(?:использования|ожидания|учета|необходимости)"
        rf"[ \t]+(?:{stem})"
    )
    for key, stem in AMENITIES.items()
}
# Ограничения `{0,2}` — защита: вокруг запятой пробелов бывает один-два, а
# неограниченные `\s*` на строке из тысяч пробелов считались бы квадратично.
_AND = r"[ \t]{0,2}(?:,|и|and|or|/|\+|&)[ \t]{0,2}"
_NEGATED_RUN = re.compile(rf"\b{NEGATOR}[ \t]{{1,2}}(?:{_ANY})(?:{_AND}(?:{_ANY}))*")


def _inside(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(start <= span[0] and span[1] <= end for start, end in spans)


def _whole_lot(text: str, span: tuple[int, int]) -> bool:
    """Отрицание говорит о жилье целиком, а не об одной его комнате."""
    return PARTIAL_BEFORE_RE.search(text[max(0, span[0] - 30) : span[0]]) is None


def _verdict(text: str, key: str) -> bool | None:
    """`False` — названо «нет», `True` — названо «есть», `None` — не названо или спор.

    Оборот, который не отрицание («без использования лифта») и не про весь лот («в
    гостиной нет кондиционера»), СЪЕДАЕТ своё слово: оно не считается ни «нет», ни «есть».
    """
    stem = _STEM[key]
    phrases = [m.span() for m in _NEGATED_RUN.finditer(text) if stem.search(m.group(0))]
    phrases += [m.span() for m in _ABSENT[key].finditer(text)]
    negated = [span for span in phrases if _whole_lot(text, span)]
    consumed = phrases + [m.span() for m in _NEUTRAL[key].finditer(text)]
    named = any(not _inside(m.span(), consumed) for m in stem.finditer(text))
    if negated and named:
        return None
    return False if negated else (True if named else None)


def _pets(text: str) -> bool | None:
    """Питомцы: оборот с названием животных и словом-подсказкой «можно» или «без».

    Отрицание проверяется первым: «не разрешены» содержит и «разреш», и «не».
    Два оборота с разным ответом — противоречие, как у удобств.
    """
    verdicts: set[bool] = set()
    for clause in CLAUSE_SPLIT.split(text):
        if not PETS_STEM.search(clause):
            continue
        if PETS_NO.search(clause):
            if not PRICED_CLAUSE.search(clause):
                verdicts.add(False)
        elif PETS_YES.search(clause):
            verdicts.add(True)
    return verdicts.pop() if len(verdicts) == 1 else None


def _kitchen(text: str) -> str | None:
    separate, shared = KITCHEN_SEPARATE.search(text), KITCHEN_SHARED.search(text)
    if bool(separate) == bool(shared):
        return None
    return "separate" if separate else "shared"


def amenity_facts(text: FactText) -> dict[str, object]:
    """Удобства, питомцы и кухня, которые пост называет; остальное отсутствует в ответе."""
    near_free = NEARBY_RE.sub(" ", text.folded)
    found: dict[str, object] = {key: _verdict(near_free, key) for key in AMENITIES}
    found["pets_allowed"] = _pets(text.folded)
    found["kitchen"] = _kitchen(text.folded)
    return {key: value for key, value in found.items() if value is not None}
