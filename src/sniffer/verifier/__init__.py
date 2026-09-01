"""Проверяльщик находок (spec-v2, раздел 3).

Три проверки, которые нельзя смешивать: grounding (карточка соответствует
исходному тексту), соответствие паспорту и живость лота.

Живость (`liveness.py`) считается по дате и без модели. Две другие живут в
`guard.py` и идут одной дешёвой моделью перед показом: отличить квартиру от
колонки JBL и вычитать «40 миллионов» из свободного текста регекспом нельзя, а
без этого бюджет клиента на телеграм-находки не влияет вовсе.
"""

from __future__ import annotations

from sniffer.verifier.guard import Verdict, screen
from sniffer.verifier.liveness import STALE_AFTER_DAYS, Liveness, LivenessVerdict, assess

__all__ = [
    "STALE_AFTER_DAYS",
    "Liveness",
    "LivenessVerdict",
    "Verdict",
    "assess",
    "screen",
]
