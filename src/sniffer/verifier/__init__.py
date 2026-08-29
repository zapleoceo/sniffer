"""Проверяльщик находок (spec-v2, раздел 3).

Три проверки, которые нельзя смешивать: grounding (карточка соответствует
исходному тексту), соответствие паспорту и живость лота. На P0 реализована
только живость — она единственная, которая нужна уже в первой выдаче: без неё
клиент идёт по ссылке на объявление, проданное два месяца назад.
"""

from __future__ import annotations

from sniffer.verifier.liveness import STALE_AFTER_DAYS, Liveness, LivenessVerdict, assess

__all__ = ["STALE_AFTER_DAYS", "Liveness", "LivenessVerdict", "assess"]
